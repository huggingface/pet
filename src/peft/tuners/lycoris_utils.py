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

import re
import warnings
from dataclasses import dataclass, field
from itertools import chain
from typing import Optional, Union

import torch
import torch.nn as nn
from tqdm import tqdm

from peft.config import PeftConfig
from peft.utils import (
    ModulesToSaveWrapper,
    _get_submodules,
)

from .tuners_utils import BaseTuner, BaseTunerLayer, check_target_module_exists


@dataclass
class LyCORISConfig(PeftConfig):
    r"""
    A base config for LyCORIS like adapters
    """
    rank_pattern: Optional[dict] = field(
        default_factory=dict,
        metadata={
            "help": (
                "The mapping from layer names or regexp expression to ranks which are different from the default rank specified by `r`. "
                "For example, `{model.decoder.layers.0.encoder_attn.k_proj: 8`}"
            )
        },
    )
    alpha_pattern: Optional[dict] = field(
        default_factory=dict,
        metadata={
            "help": (
                "The mapping from layer names or regexp expression to alphas which are different from the default alpha specified by `alpha`. "
                "For example, `{model.decoder.layers.0.encoder_attn.k_proj: 32`}"
            )
        },
    )


class LyCORISLayer(BaseTunerLayer):
    r"""
    A base layer for LyCORIS like adapters
    """

    def __init__(self):
        # Tuner info
        self._disable_adapters = False
        self.merged_adapters = []

    @property
    def merged(self) -> bool:
        return bool(self.merged_adapters)

    def update_layer(self, adapter_name: str, r: int, alpha: float, **kwargs):
        ...


class LyCORISTuner(BaseTuner):
    r"""
    A base tuner for LyCORIS like adapters
    """

    prefix: str

    def __init__(self, model, config, adapter_name):
        super().__init__(model, config, adapter_name)

    def __getattr__(self, name: str):
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self.model, name)

    @staticmethod
    def _check_target_module_exists(config, key):
        return check_target_module_exists(config, key)

    def _create_and_replace(
        self,
        config: LyCORISConfig,
        adapter_name: str,
        target: Union[LyCORISLayer, nn.Module],
        target_name,
        parent,
        current_key,
        **optional_kwargs,
    ):
        """
        A private method to create and replace the target module with the adapter module.
        """

        # Regexp matching - Find key which matches current target_name in patterns provided
        pattern_keys = list(chain(config.rank_pattern.keys(), config.alpha_pattern.keys()))
        target_name_key = next(filter(lambda key: re.match(f"(.*\.)?{key}$", current_key), pattern_keys), target_name)

        kwargs = config.to_dict()
        kwargs["r"] = config.rank_pattern.get(target_name_key, config.r)
        kwargs["alpha"] = config.alpha_pattern.get(target_name_key, config.alpha)

        if isinstance(target, LyCORISLayer):
            target.update_layer(adapter_name, **kwargs)
        else:
            new_module = self._create_new_module(config, adapter_name, target, **kwargs)
            self._replace_module(parent, target_name, new_module, target)

    @staticmethod
    def _create_new_module(config: LyCORISConfig, adapter_name: str, target: nn.Module, **kwargs) -> LyCORISLayer:
        ...

    def _mark_only_adapters_as_trainable(self) -> None:
        for n, p in self.model.named_parameters():
            if self.prefix not in n:
                p.requires_grad = False

    @staticmethod
    def _prepare_adapter_config(peft_config, model_config):
        if peft_config.target_modules is None:
            raise ValueError("Please specify `target_modules` in `peft_config`")
        return peft_config

    @classmethod
    def _replace_module(cls, parent, child_name, new_module, child):
        setattr(parent, child_name, new_module)
        # It's not necessary to set requires_grad here, as that is handled by
        # _mark_only_adapters_as_trainable
        new_module.weight = child.weight
        if hasattr(child, "bias"):
            new_module.bias = child.bias

        if getattr(child, "state", None) is not None:
            new_module.state = child.state
            new_module.to(child.weight.device)

        # dispatch to correct device
        for name, module in new_module.named_modules():
            if cls.prefix in name:
                module.to(child.weight.device)

    def _set_adapter_layers(self, enabled=True):
        for module in self.model.modules():
            if isinstance(module, (BaseTunerLayer, ModulesToSaveWrapper)):
                module.enable_adapters(enabled)

    def _unload_and_optionally_merge(self, merge=True, progressbar: bool = False):
        if merge:
            if getattr(self.model, "quantization_method", None) == "gptq":
                raise ValueError("Cannot merge LOHA layers when the model is gptq quantized")

        key_list = [key for key, _ in self.model.named_modules() if "hada" not in key]
        desc = "Unloading " + ("and merging " if merge else "") + "model"
        for key in tqdm(key_list, disable=not progressbar, desc=desc):
            try:
                parent, target, target_name = _get_submodules(self.model, key)
            except AttributeError:
                continue
            if isinstance(target, LyCORISLayer):
                if isinstance(target, nn.Conv2d):
                    new_module = torch.nn.Conv2d(
                        target.in_channels,
                        target.out_channels,
                        kernel_size=target.kernel_size,
                        stride=target.stride,
                        padding=target.padding,
                        dilation=target.dilation,
                    )
                elif isinstance(target, nn.Linear):
                    bias = target.bias is not None
                    new_module = torch.nn.Linear(
                        target.in_features,
                        target.out_features,
                        bias=bias,
                        device=target.weight.device,
                    )
                else:
                    raise ValueError(
                        "Cannot convert current module to torch module, currently only adapters for nn.Linear and nn.Conv2d are supported"
                    )
                if merge:
                    target.merge()
                self._replace_module(parent, target_name, new_module, target)

            # save any additional trainable modules part of `modules_to_save`
            if isinstance(target, ModulesToSaveWrapper):
                setattr(parent, target_name, target.modules_to_save[target.active_adapter])

        return self.model

    def enable_adapter_layers(self):
        self._set_adapter_layers(enabled=True)

    def disable_adapter_layers(self):
        self._set_adapter_layers(enabled=False)

    def merge_and_unload(self, progressbar: bool = False):
        return self._unload_and_optionally_merge(progressbar=progressbar)

    def set_adapter(self, adapter_name):
        for module in self.model.modules():
            if isinstance(module, LyCORISLayer):
                if module.merged:
                    warnings.warn("Adapter cannot be set when the model is merged. Unmerging the model first.")
                    module.unmerge()
                module.set_adapter(adapter_name)
