# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import os
from dataclasses import dataclass

import torch

from torchrl.data import (
    DEVICE_TYPING,
    ReplayBuffer,
    TensorDictPrioritizedReplayBuffer,
    TensorDictReplayBuffer,
)

__all__ = ["make_replay_buffer"]

from torchrl.data.replay_buffers.storages import LazyMemmapStorage


def make_replay_buffer(device: DEVICE_TYPING, cfg: "DictConfig") -> ReplayBuffer:
    """Builds a replay buffer using the config built from ReplayArgsConfig."""
    device = torch.device(device)
    if not cfg.prb:
        buffer = TensorDictReplayBuffer(
            cfg.buffer_size,
            collate_fn=lambda x: x,
            pin_memory=device != torch.device("cpu"),
            prefetch=3,
            storage=LazyMemmapStorage(
                cfg.buffer_size,
                # SCRATCH_DIR may depend upon job id etc, hence we
                scratch_dir=os.environ.get("SCRATCH_DIR", None),
            ),
        )
    else:
        buffer = TensorDictPrioritizedReplayBuffer(
            cfg.buffer_size,
            alpha=0.7,
            beta=0.5,
            collate_fn=lambda x: x,
            pin_memory=device != torch.device("cpu"),
            prefetch=3,
            storage=LazyMemmapStorage(
                cfg.buffer_size,
                scratch_dir=os.environ.get("SCRATCH_DIR", None),
            ),
        )
    return buffer


@dataclass
class ReplayArgsConfig:
    buffer_size: int = 1000000
    # buffer size, in number of frames stored. Default=1e6
    prb: bool = False
    # whether a Prioritized replay buffer should be used instead of a more basic circular one.
