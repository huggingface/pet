# coding=utf-8
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

import torch

from ..lycoris_utils import LyCORISTuner
from .layer import Conv2d, Linear, LoHaLayer


class LoHaModel(LyCORISTuner):
    """
    Creates Low-Rank Hadamard Product model from a pretrained model. The method is partially described in
    https://arxiv.org/abs/2108.06098 Current implementation heavily borrows from
    https://github.com/KohakuBlueleaf/LyCORIS/blob/eb460098187f752a5d66406d3affade6f0a07ece/lycoris/modules/loha.py

    Args:
        model (`torch.nn.Module`): The model to which the adapter tuner layers will be attached.
        config ([`LoHaConfig`]): The configuration of the LoHa model.
        adapter_name (`str`): The name of the adapter, defaults to `"default"`.

    Returns:
        `torch.nn.Module`: The LoHa model.

    Example:
        ```py
        >>> from diffusers import StableDiffusionPipeline
        >>> from peft import LoHaModel, LoHaConfig

        >>> config_te = LoHaConfig(
        ...     r=8,
        ...     lora_alpha=32,
        ...     target_modules=["k_proj", "q_proj", "v_proj", "out_proj", "fc1", "fc2"],
        ...     rank_dropout=0.0,
        ...     module_dropout=0.0,
        ...     init_weights=True,
        ... )
        >>> config_unet = LoHaConfig(
        ...     r=8,
        ...     lora_alpha=32,
        ...     target_modules=[
        ...         "proj_in",
        ...         "proj_out",
        ...         "to_k",
        ...         "to_q",
        ...         "to_v",
        ...         "to_out.0",
        ...         "ff.net.0.proj",
        ...         "ff.net.2",
        ...     ],
        ...     rank_dropout=0.0,
        ...     module_dropout=0.0,
        ...     init_weights=True,
        ...     use_effective_conv2d=True,
        ... )

        >>> model = StableDiffusionPipeline.from_pretrained("runwayml/stable-diffusion-v1-5")
        >>> model.text_encoder = LoHaModel(model.text_encoder, config_te, "default")
        >>> model.unet = LoHaModel(model.unet, config_unet, "default")
        ```

    **Attributes**:
        - **model** ([`~torch.nn.Module`]) -- The model to be adapted.
        - **peft_config** ([`LoHaConfig`]): The configuration of the LoHa model.
    """

    prefix: str = "hada_"

    @staticmethod
    def _create_new_module(config, adapter_name, target, **kwargs) -> LoHaLayer:
        if isinstance(target, torch.nn.Conv2d):
            new_module = Conv2d(
                target.in_channels,
                target.out_channels,
                target.weight.size()[2:],
                stride=target.stride,
                padding=target.padding,
                dilation=target.dilation,
                groups=target.groups,
                bias=target.bias is not None,
                padding_mode=target.padding_mode,
                device=target.weight.device,
                dtype=target.weight.dtype,
                adapter_name=adapter_name,
                **kwargs,
            )
        elif isinstance(target, torch.nn.Linear):
            new_module = Linear(
                target.in_features,
                target.out_features,
                bias=target.bias is not None,
                device=target.weight.device,
                dtype=target.weight.dtype,
                adapter_name=adapter_name,
                **kwargs,
            )
        else:
            raise ValueError(
                "Target module not found, currently only adapters for nn.Linear and nn.Conv2d are supported"
            )
        return new_module
