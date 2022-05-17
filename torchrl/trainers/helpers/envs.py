# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from argparse import ArgumentParser, Namespace
from typing import Callable, Optional, Union

import torch

from torchrl.envs import DMControlEnv, GymEnv, ParallelEnv, RetroEnv
from torchrl.envs.common import _EnvClass
from torchrl.envs.env_creator import env_creator, EnvCreator
from torchrl.envs.transforms import (
    CatFrames,
    CatTensors,
    DoubleToFloat,
    FiniteTensorDictCheck,
    GrayScale,
    NoopResetEnv,
    ObservationNorm,
    Resize,
    RewardScaling,
    ToTensorImage,
    TransformedEnv,
    VecNorm,
)
from torchrl.envs.transforms.transforms import gSDENoise
from torchrl.record.recorder import VideoRecorder

__all__ = [
    "correct_for_frame_skip",
    "transformed_env_constructor",
    "parallel_env_constructor",
    "get_stats_random_rollout",
    "parser_env_args",
]

LIBS = {
    "gym": GymEnv,
    "retro": RetroEnv,
    "dm_control": DMControlEnv,
}


def correct_for_frame_skip(args: Namespace) -> Namespace:
    """
    Correct the arguments for the input frame_skip, by dividing all the arguments that reflect a count of frames by the
    frame_skip.
    This is aimed at avoiding unknowingly over-sampling from the environment, i.e. targetting a total number of frames
    of 1M but actually collecting frame_skip * 1M frames.

    Args:
        args (argparse.Namespace): Namespace containing some frame-counting argument, including:
            "max_frames_per_traj", "total_frames", "frames_per_batch", "record_frames", "annealing_frames",
            "init_random_frames", "init_env_steps"

    Returns:
         the input Namespace, modified in-place.

    """
    # Adapt all frame counts wrt frame_skip
    if args.frame_skip != 1:
        fields = [
            "max_frames_per_traj",
            "total_frames",
            "frames_per_batch",
            "record_frames",
            "annealing_frames",
            "init_random_frames",
            "init_env_steps",
            "noops",
        ]
        for field in fields:
            if hasattr(args, field):
                setattr(args, field, getattr(args, field) // args.frame_skip)
    return args


def transformed_env_constructor(
    args: Namespace,
    video_tag: str = "",
    writer: Optional["SummaryWriter"] = None,
    stats: Optional[dict] = None,
    norm_obs_only: bool = False,
    use_env_creator: bool = True,
    custom_env_maker: Optional[Callable] = None,
) -> Union[Callable, EnvCreator]:
    """
    Returns an environment creator from an argparse.Namespace built with the appropriate parser constructor.

    Args:
        args (argparse.Namespace): script arguments originating from the parser built with parser_env_args
        video_tag (str, optional): video tag to be passed to the SummaryWriter object
        writer (SummaryWriter, optional): tensorboard writer associated with the script
        stats (dict, optional): a dictionary containing the `loc` and `scale` for the `ObservationNorm` transform
        norm_obs_only (bool, optional): If `True` and `VecNorm` is used, the reward won't be normalized online.
            Default is `False`.
        use_env_creator (bool, optional): wheter the `EnvCreator` class should be used. By using `EnvCreator`,
            one can make sure that running statistics will be put in shared memory and accessible for all workers
            when using a `VecNorm` transform. Default is `True`.
        custom_env_maker (callable, optional): if your env maker is not part
            of torchrl env wrappers, a custom callable
            can be passed instead. In this case it will override the
            constructor retrieved from `args`.
    """

    def make_transformed_env() -> TransformedEnv:
        env_name = args.env_name
        env_task = args.env_task
        env_library = LIBS[args.env_library]
        frame_skip = args.frame_skip
        from_pixels = args.from_pixels
        vecnorm = args.vecnorm
        norm_rewards = vecnorm and args.norm_rewards
        _norm_obs_only = norm_obs_only or not norm_rewards
        reward_scaling = args.reward_scaling
        reward_loc = args.reward_loc

        if custom_env_maker is None:
            env_kwargs = {
                "envname": env_name,
                "device": "cpu",
                "frame_skip": frame_skip,
                "from_pixels": from_pixels or len(video_tag),
                "pixels_only": from_pixels,
            }
            if env_library is DMControlEnv:
                env_kwargs.update({"taskname": env_task})
            env = env_library(**env_kwargs)
        else:
            env = custom_env_maker()

        env = TransformedEnv(env)

        if len(video_tag):
            env.append_transform(
                VideoRecorder(
                    writer=writer,
                    tag=f"{video_tag}_{env_name}_video",
                ),
            )

        if args.noops:
            env.append_transform(NoopResetEnv(env, args.noops))
        if from_pixels:
            env.append_transform(ToTensorImage())
            env.append_transform(Resize(84, 84))
            env.append_transform(GrayScale())
            env.append_transform(CatFrames(keys=["next_pixels"]))
            env.append_transform(
                ObservationNorm(loc=-1.0, scale=2.0, keys=["next_pixels"])
            )
        if norm_rewards:
            reward_scaling = 1.0
            reward_loc = 0.0
        if norm_obs_only:
            reward_scaling = 1.0
            reward_loc = 0.0
        if reward_scaling is not None:
            env.append_transform(RewardScaling(reward_loc, reward_scaling))

        double_to_float_list = []
        if env_library is DMControlEnv:
            double_to_float_list += [
                "reward",
                "action",
            ]  # DMControl requires double-precision
        if not from_pixels:
            selected_keys = [
                key for key in env.observation_spec.keys() if "pixels" not in key
            ]

            # even if there is a single tensor, it'll be renamed in "next_observation_vector"
            out_key = "next_observation_vector"
            env.append_transform(CatTensors(keys=selected_keys, out_key=out_key))

            if not vecnorm:
                if stats is None:
                    _stats = {"loc": 0.0, "scale": 1.0}
                else:
                    _stats = stats
                env.append_transform(
                    ObservationNorm(**_stats, keys=[out_key], standard_normal=True)
                )
            else:
                env.append_transform(
                    VecNorm(
                        keys=[out_key, "reward"] if not _norm_obs_only else [out_key],
                        decay=0.9999,
                    )
                )

            double_to_float_list.append(out_key)
            env.append_transform(DoubleToFloat(keys=double_to_float_list))

            if hasattr(args, "catframes") and args.catframes:
                env.append_transform(
                    CatFrames(N=args.catframes, keys=[out_key], cat_dim=-1)
                )

            if hasattr(args, "gSDE") and args.gSDE:
                state_dim = env.observation_spec[out_key].shape[-1]
                env.append_transform(
                    gSDENoise(
                        action_dim=env.action_spec.shape[-1],
                        observation_key=out_key,
                        state_dim=state_dim,
                    )
                )

        else:
            env.append_transform(DoubleToFloat(keys=double_to_float_list))
            if hasattr(args, "gSDE") and args.gSDE:
                raise RuntimeError("gSDE not compatible with from_pixels=True")

        env.append_transform(FiniteTensorDictCheck())
        return env

    if use_env_creator:
        return env_creator(make_transformed_env)
    return make_transformed_env


def parallel_env_constructor(args: Namespace, **kwargs) -> Union[ParallelEnv, EnvCreator]:
    """Returns a parallel environment from an argparse.Namespace built with the appropriate parser constructor.

    Args:
        args (argparse.Namespace): script arguments originating from the parser built with parser_env_args
        kwargs: keyword arguments for the `transformed_env_constructor` method.
    """
    if args.env_per_collector == 1:
        kwargs.update({"args": args, "use_env_creator": True})
        make_transformed_env = transformed_env_constructor(**kwargs)
        return make_transformed_env
    kwargs.update({"args": args, "use_env_creator": True})
    make_transformed_env = transformed_env_constructor(**kwargs)
    env = ParallelEnv(
        num_workers=args.env_per_collector,
        create_env_fn=make_transformed_env,
        create_env_kwargs=None,
        pin_memory=args.pin_memory,
    )
    return env


def get_stats_random_rollout(
    args: Namespace, proof_environment: _EnvClass, key: Optional[str] = None
):
    print("computing state stats")
    if not hasattr(args, "init_env_steps"):
        raise AttributeError("init_env_steps missing from arguments.")

    n = 0
    td_stats = []
    while n < args.init_env_steps:
        _td_stats = proof_environment.rollout(n_steps=args.init_env_steps)
        n += _td_stats.numel()
        td_stats.append(_td_stats)
    td_stats = torch.cat(td_stats, 0)

    if key is None:
        keys = list(proof_environment.observation_spec.keys())
        key = keys.pop()
        if len(keys):
            raise RuntimeError(
                f"More than one key exists in the observation_specs: {[key] + keys} were found, "
                "thus get_stats_random_rollout cannot infer which to compute the stats of."
            )
    m = td_stats.get(key).mean(dim=0)
    s = td_stats.get(key).std(dim=0).clamp_min(1e-5)
    print(
        f"stats computed for {td_stats.numel()} steps. Got: \n"
        f"loc = {m}, \n"
        f"scale: {s}"
    )
    if not torch.isfinite(m).all():
        raise RuntimeError("non-finite values found in mean")
    if not torch.isfinite(s).all():
        raise RuntimeError("non-finite values found in sd")
    stats = {"loc": m, "scale": s}
    return stats


def parser_env_args(parser: ArgumentParser) -> ArgumentParser:
    """
    Populates the argument parser to build an environment constructor.

    Args:
        parser (ArgumentParser): parser to be populated.

    """

    parser.add_argument(
        "--env_library",
        "--env-library",
        type=str,
        default="gym",
        choices=list(LIBS.keys()),
        help="env_library used for the simulated environment. Default=gym",
    )
    parser.add_argument(
        "--env_name",
        "--env-name",
        type=str,
        default="Humanoid-v2",
        help="name of the environment to be created. Default=Humanoid-v2",
    )
    parser.add_argument(
        "--env_task",
        "--env-task",
        type=str,
        default="",
        help="task (if any) for the environment. Default=run",
    )
    parser.add_argument(
        "--from_pixels",
        "--from-pixels",
        action="store_true",
        help="whether the environment output should be state vector(s) (default) or the pixels.",
    )
    parser.add_argument(
        "--frame_skip",
        "--frame-skip",
        type=int,
        default=1,
        help="frame_skip for the environment. Note that this value does NOT impact the buffer size,"
        "maximum steps per trajectory, frames per batch or any other factor in the algorithm,"
        "e.g. if the total number of frames that has to be computed is 50e6 and the frame skip is 4,"
        "the actual number of frames retrieved will be 200e6. Default=1.",
    )
    parser.add_argument(
        "--reward_scaling", "--reward-scaling", type=float, help="scale of the reward."
    )
    parser.add_argument(
        "--reward_loc",
        "--reward-loc",
        type=float,
        help="location of the reward.",
        default=0.0,
    )
    parser.add_argument(
        "--init_env_steps",
        "--init-env-steps",
        type=int,
        default=1000,
        help="number of random steps to compute normalizing constants",
    )
    parser.add_argument(
        "--vecnorm",
        action="store_true",
        help="Normalizes the environment observation and reward outputs with the running statistics "
        "obtained across processes.",
    )
    parser.add_argument(
        "--norm_rewards",
        "--norm-rewards",
        action="store_true",
        help="If True, rewards will be normalized on the fly. This may interfere with SAC update rule and "
        "should be used cautiously.",
    )
    parser.add_argument(
        "--no_norm_stats",
        "--no-norm-stats",
        action="store_false",
        dest="norm_stats",
        help="Deactivates the normalization based on random collection of data.",
    )
    parser.add_argument(
        "--noops",
        type=int,
        default=0,
        help="number of random steps to do after reset. Default is 0",
    )
    parser.add_argument(
        "--catframes",
        type=int,
        default=0,
        help="Number of frames to concatenate through time. Default is 0 (do not use CatFrames).",
    )
    parser.add_argument(
        "--max_frames_per_traj",
        "--max-frames-per-traj",
        type=int,
        default=1000,
        help="Number of steps before a reset of the environment is called (if it has not been flagged as "
        "done before). ",
    )

    return parser
