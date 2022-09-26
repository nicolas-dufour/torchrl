# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from typing import Optional

import torch

from torchrl.data import TensorDict
from torchrl.envs.utils import set_exploration_mode
from torchrl.envs.utils import step_tensordict
from torchrl.modules import TensorDictModule
from torchrl.objectives.costs.common import LossModule
from torchrl.objectives.costs.utils import hold_out_net, distance_loss
from torchrl.objectives.returns.functional import vec_td_lambda_return_estimate
from torchrl.envs.model_based.dreamer import DreamerEnv

class DreamerModelLoss(LossModule):
    """Dreamer Model Loss

    Computes the loss of the dreamer world model. The loss is composed of the kl divergence between the prior and posterior of the RSSM,
    the reconstruction loss between the observation and the reconstructed observation and the reward loss between the true reward and the predicted reward.

    Reference: https://arxiv.org/abs/1912.01603

    Args:
        world_model (TensorDictModule): the world model.
        cfg (DictConfig): the config file.
        lambda_kl (float): the weight of the kl divergence loss.
        lambda_reco (float): the weight of the reconstruction loss.
        lambda_reward (float): the weight of the reward loss.
        reco_loss (Optional[str]): the reconstruction loss.
        reward_loss (Optional[str]): the reward loss.
        free_nats (int): the free nats.
        inversed_free_nats (bool): if True, the free nats are inversed. First we average the kl divergence and then we clamp it to the free nats.
    """

    def __init__(
        self,
        world_model: TensorDictModule,
        cfg: "DictConfig",
        lambda_kl: float = 1.0,
        lambda_reco: float = 1.0,
        lambda_reward: float = 1.0,
        reco_loss: Optional[str] = None,
        reward_loss: Optional[str] = None,
        free_nats: int = 3,
        inversed_free_nats: bool = False,
    ):
        super().__init__()
        self.world_model = world_model
        self.cfg = cfg
        self.reco_loss = reco_loss if reco_loss is not None else "l2"
        self.reward_loss = reward_loss if reward_loss is not None else "l2"
        self.lambda_kl = lambda_kl
        self.lambda_reco = lambda_reco
        self.lambda_reward = lambda_reward
        self.free_nats = free_nats
        self.inversed_free_nats = inversed_free_nats

    def forward(self, tensordict: TensorDict) -> torch.Tensor:
        tensordict = tensordict.clone(recurse=False)

        # prepare tensordict: remove time in batch dimensions
        tensordict.batch_size = tensordict.batch_size[:1]
        # take the first tensor for prev_posterior_state and prev_belief
        tensordict["prev_posterior_state"] = torch.zeros_like(tensordict["prev_posterior_state"][:, 0])
        tensordict["prev_belief"] = torch.zeros_like(tensordict["prev_belief"][:, 0])
        tensordict["true_reward"] = tensordict["reward"]
        del tensordict["reward"]

        tensordict = self.world_model(tensordict)
        # compute model loss
        kl_loss = self.kl_loss(
            tensordict.get("prior_means"),
            tensordict.get("prior_stds"),
            tensordict.get("posterior_means"),
            tensordict.get("posterior_stds"),
        )
        reco_loss = (
            distance_loss(
                tensordict.get("pixels"),
                tensordict.get("reco_pixels"),
                self.reco_loss,
            )
            .sum((-1, -2, -3))
            .mean()
        )
        reward_loss = distance_loss(
            tensordict.get("true_reward"),
            tensordict.get("reward"),
            self.reward_loss,
        ).mean()
        loss = (
            self.lambda_kl * kl_loss
            + self.lambda_reco * reco_loss
            + self.lambda_reward * reward_loss
        )
        return (
            TensorDict(
                {
                    "loss_world_model": loss,
                    "kl_model_loss": kl_loss,
                    "reco_model_loss": reco_loss,
                    "reward_model_loss": reward_loss,
                },
                [],
            ),
            tensordict.detach(),
        )

    def kl_loss(self, prior_mean: torch.Tensor, prior_std: torch.Tensor, posterior_mean: torch.Tensor, posterior_std: torch.Tensor) -> torch.Tensor:
        kl = (
            torch.log(prior_std / posterior_std)
            + (posterior_std ** 2 + (prior_mean - posterior_mean) ** 2)
            / (2 * prior_std ** 2)
            - 0.5
        )
        if self.inversed_free_nats:
            kl = kl.sum(-1).mean().clamp_min(self.free_nats)
        else:
            kl = kl.sum(-1).clamp_min(self.free_nats).mean()
        return kl


class DreamerActorLoss(LossModule):
    """Dreamer Actor Loss

    Computes the loss of the dreamer actor. The actor loss is computed as the negative lambda return average.

    Reference: https://arxiv.org/abs/1912.01603

    Args:
        actor_model (TensorDictModule): the actor model.
        value_model (TensorDictModule): the value model.
        model_based_env (DreamerEnv): the model based environment.
        cfg (DictConfig): the config file.
        gamma (float, optional): the discount factor.
        lmbda (float, optional): the lambda factor.
        discount_loss (bool, optional): if True, the loss is discounted with a gamma discount factor.
    """
    def __init__(
        self,
        actor_model: TensorDictModule,
        value_model: TensorDictModule,
        model_based_env: DreamerEnv,
        cfg: "DictConfig",
        gamma: int =0.99,
        lmbda: int =0.95,
        discount_loss: bool=True,
    ):
        super().__init__()
        self.actor_model = actor_model
        self.value_model = value_model
        self.model_based_env = model_based_env
        self.cfg = cfg
        self.gamma = gamma
        self.lmbda = lmbda
        self.discount_loss = discount_loss

    def forward(self, tensordict: TensorDict) -> torch.Tensor:
        with torch.no_grad():
            tensordict = tensordict.select(
                "posterior_state", "belief", "reward"
            )

            tensordict.batch_size = [
                tensordict.shape[0],
                tensordict.get("belief").shape[1],
            ]
            tensordict.rename_key("posterior_state", "prior_state")
            tensordict = tensordict.view(-1).detach()
        with hold_out_net(self.model_based_env), set_exploration_mode("random"):
            tensordict = self.model_based_env.rollout(
                max_steps=self.cfg.imagination_horizon,
                policy=self.actor_model,
                auto_reset=False,
                tensordict=tensordict,
            )
            tensordict = step_tensordict(
                tensordict,
                keep_other=True,
                exclude_reward=False,
                exclude_action=False,
            )
            with hold_out_net(self.value_model):
                tensordict = self.value_model(tensordict)

        lambda_target = self.lambda_target(
            tensordict.get("reward"), tensordict.get("predicted_value")
        )
        tensordict = tensordict[:, :-1]
        tensordict.set("lambda_target", lambda_target)

        if self.discount_loss:
            discount = self.gamma * torch.ones_like(lambda_target, device=tensordict.device)
            discount[:, 0] = 1
            discount = discount.cumprod(dim=1).detach()
            actor_loss = -(lambda_target * discount).mean()
        else:
            actor_loss = -lambda_target.mean()
        return (
            TensorDict(
                {
                    "loss_actor": actor_loss,
                },
                batch_size=[],
            ),
            tensordict.detach(),
        )

    def lambda_target(self, reward:  torch.Tensor, value: torch.Tensor)-> torch.Tensor:
        done = torch.zeros(reward.shape, dtype=torch.bool, device=reward.device)
        return vec_td_lambda_return_estimate(
            self.gamma, self.lmbda, value[:, 1:], reward[:, :-1], done[:, :-1]
        )


class DreamerValueLoss(LossModule):
    """Dreamer Value Loss

    Computes the loss of the dreamer value model. The value loss is computed as the mean squared error between the predicted value and the lambda target.

    Reference: https://arxiv.org/abs/1912.01603

    Args:
        value_model (TensorDictModule): the value model.
        value_loss (str, optional): the loss to use for the value loss.
        gamma (float, optional): the discount factor.
        discount_loss (bool, optional): if True, the loss is discounted with a gamma discount factor.
    """
    def __init__(
        self,
        value_model: TensorDictModule,
        value_loss: Optional[str] = None,
        gamma: int=0.99,
        discount_loss: bool=True,
    ):
        super().__init__()
        self.value_model = value_model
        self.value_loss = value_loss if value_loss is not None else "l2"
        self.gamma = gamma
        self.discount_loss = discount_loss

    def forward(self, tensordict) -> torch.Tensor:
        tensordict = self.value_model(tensordict)
        if self.discount_loss:
            discount = self.gamma * torch.ones_like(
                tensordict.get("lambda_target"), device=tensordict.device
            )
            discount[:, 0] = 1
            discount = discount.cumprod(dim=1).detach()
            value_loss = (
                (
                    discount
                    * distance_loss(
                        tensordict.get("predicted_value"),
                        tensordict.get("lambda_target"),
                        self.value_loss,
                    )
                )
                .sum((-1, -2))
                .mean()
            )
        else:
            value_loss = (
                distance_loss(
                    tensordict.get("predicted_value"),
                    tensordict.get("lambda_target"),
                    self.value_loss,
                )
                .sum((-1, -2))
                .mean()
            )

        return (
            TensorDict(
                {
                    "loss_value": value_loss,
                },
                batch_size=[],
            ),
            tensordict.detach(),
        )
