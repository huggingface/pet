# Copyright 2023-present the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from contextlib import contextmanager

import torch
from transformers.integrations import is_deepspeed_zero3_enabled


@contextmanager
def gather_params_ctx(module: torch.nn.Module):
    """Call DeepSpeed GatheredParameters context manager if DeepSpeed is enabled, otherwise do nothing."""
    if not is_deepspeed_zero3_enabled():
        yield
        return

    import deepspeed

    params_to_gather = module.parameters()
    with deepspeed.zero.GatheredParameters(params_to_gather, modifier_rank=0):
        yield
    return
