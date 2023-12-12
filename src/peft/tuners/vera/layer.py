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

import warnings
from typing import Optional, List, Union

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.pytorch_utils import Conv1D

from peft.tuners.tuners_utils import BaseTunerLayer
from peft.utils.other import transpose


class VeraLayer(BaseTunerLayer):
    # List all names of layers that may contain adapter weights
    adapter_layer_names = ("vera_lambda_b", "vera_lambda_d")

    def __init__(self, base_layer: nn.Module, **kwargs):
        self.base_layer = base_layer
        self.r = {}
        self.vera_dropout = nn.ModuleDict({})

        # For storing vector scale
        self.vera_lambda_b = nn.ParameterDict({})
        self.vera_lambda_d = nn.ParameterDict({})
        # Mark the weight as unmerged
        self._disable_adapters = False
        self.merged_adapters = []

        base_layer = self.get_base_layer()
        if isinstance(base_layer, nn.Linear):
            in_features, out_features = base_layer.in_features, base_layer.out_features
        elif isinstance(base_layer, nn.Embedding):
            in_features, out_features = base_layer.num_embeddings, base_layer.embedding_dim
        elif isinstance(base_layer, Conv1D):
            in_features, out_features = (
                base_layer.weight.ds_shape if hasattr(base_layer.weight, "ds_shape") else base_layer.weight.shape
            )

        self.in_features = in_features
        self.out_features = out_features
        self.kwargs = kwargs

    @property
    def merged(self) -> bool:
        return bool(self.merged_adapters)

    def update_layer(self, adapter_name, r, vera_dropout, init_vera_weights, d_initial: float = 1.0):
        if r <= 0:
            raise ValueError(f"`r` should be a positive integer value but the value passed is {r}")
        self.r[adapter_name] = r
        if vera_dropout > 0.0:
            vera_dropout_layer = nn.Dropout(p=vera_dropout)
        else:
            vera_dropout_layer = nn.Identity()

        self.vera_dropout.update(nn.ModuleDict({adapter_name: vera_dropout_layer}))
        # Actual trainable parameters
        self.vera_lambda_b[adapter_name] = nn.Parameter(torch.ones(self.out_features), requires_grad=True)
        self.vera_lambda_d[adapter_name] = nn.Parameter(torch.ones(r), requires_grad=True)

        if init_vera_weights:
            self.reset_vera_parameters(adapter_name, d_initial=d_initial)

        weight = getattr(self.get_base_layer(), "weight", None)
        if weight is not None:
            # the layer is already completely initialized, this is an update
            if weight.dtype.is_floating_point or weight.dtype.is_complex:
                self.to(weight.device, dtype=weight.dtype)
            else:
                self.to(weight.device)
        self.set_adapter(self.active_adapters)

    def update_layer_embedding(self, adapter_name, r, vera_dropout, init_vera_weights, d_initial: float = 1.0):
        if r <= 0:
            raise ValueError(f"`r` should be a positive integer value but the value passed is {r}")
        self.r[adapter_name] = r
        if vera_dropout > 0.0:
            vera_dropout_layer = nn.Dropout(p=vera_dropout)
        else:
            vera_dropout_layer = nn.Identity()

        self.vera_dropout[adapter_name] = vera_dropout_layer
        # Actual trainable parameters
        self.vera_lambda_b[adapter_name] = nn.Parameter(torch.ones(self.out_features), requires_grad=True)
        self.vera_lambda_d[adapter_name] = nn.Parameter(torch.ones(r), requires_grad=True)

        if init_vera_weights:
            self.reset_vera_parameters(adapter_name, d_initial=d_initial)

        weight = getattr(self.get_base_layer(), "weight", None)
        if weight is not None:
            # the layer is already completely initialized, this is an update
            self.to(self.weight.device, dtype=weight.dtype)

    def reset_vera_parameters(self, adapter_name, d_initial: float = 1.0):
        if adapter_name in self.vera_lambda_d.keys():
            with torch.no_grad():
                nn.init.zeros_(self.vera_lambda_d[adapter_name]).fill_(d_initial)
                nn.init.zeros_(self.vera_lambda_b[adapter_name])


# Below was based on 'src/peft/tuners/lora/layer.py
# Which was in turn based on https://github.com/microsoft/LoRA/blob/main/loralib/layers.py


#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------


class Linear(nn.Linear, VeraLayer):
    # Vera implemented in a dense layer
    def __init__(
        self,
        base_layer,
        adapter_name: str,
        r: int = 0,
        vera_dropout: float = 0.0,
        fan_in_fan_out: bool = False,  # Set this to True if the layer to replace stores weight like (fan_in, fan_out)
        is_target_conv_1d_layer: bool = False,
        init_vera_weights: Union[bool, str] = True,
        d_initial: float = 1.0,
        **kwargs,
    ) -> None:
        # this gets the init from nn.Linear's super perspective, i.e.
        # nn.Module.__init__, which should always be called
        super(nn.Linear, self).__init__()
        VeraLayer.__init__(self, base_layer, **kwargs)
        self.fan_in_fan_out = fan_in_fan_out

        self._active_adapter = adapter_name
        self.update_layer(adapter_name, r, vera_dropout, init_vera_weights, d_initial=d_initial)
        self.is_target_conv_1d_layer = is_target_conv_1d_layer

    def merge(self, vera_A: torch.Tensor, vera_B: torch.Tensor, adapter_names: Optional[List[str]] = None, safe_merge: bool = False) -> None:
        """
        Merge the active adapter weights into the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
            adapter_names (`List[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        if self.merged:
            warnings.warn(
                f"Already following adapters were merged {','.join(self.merged_adapters)}. "
                f"You are now additionally merging {','.join(self.active_adapters)}."
            )

        if adapter_names is None:
            adapter_names = self.active_adapter

        for active_adapter in adapter_names:
            if active_adapter in self.vera_lambda_d.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.clone()

                    orig_weights += self.get_delta_weight(active_adapter, vera_A, vera_B)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )

                    base_layer.weight.data = orig_weights
                else:
                    base_layer.weight.data += self.get_delta_weight(active_adapter, vera_A, vera_B)
                self.merged_adapters.append(active_adapter)

    def unmerge(self, vera_A: torch.Tensor, vera_B: torch.Tensor) -> None:
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.vera_lambda_d.keys():
                self.get_base_layer().weight.data -= self.get_delta_weight(active_adapter, vera_A, vera_B)

    def get_delta_weight(self, adapter, vera_A: torch.Tensor, vera_B: torch.Tensor) -> torch.Tensor:
        """
        Compute the delta weight for the given adapter.

        Args:
            adapter (str):
                The name of the adapter for which the delta weight should be computed.
        """
        device = vera_B.device
        dtype = vera_B.dtype

        # In case users wants to merge the adapter weights that are in
        # float16 while being on CPU, we need to cast the weights to float32, perform the merge and then cast back to
        # float16 because the `@` and matmul operation in general is not supported in torch + cpu + fp16.
        cast_to_fp32 = device.type == "cpu" and dtype == torch.float16

        lambda_d = self.vera_lambda_d[adapter]
        lambda_b = self.vera_lambda_b[adapter]

        if cast_to_fp32:
            vera_A = vera_A.float()
            vera_B = vera_B.float()
            lambda_d = lambda_d.float()
            lambda_b = lambda_b.float()

        lambda_b = lambda_b.unsqueeze(-1)
        lambda_d = lambda_d.unsqueeze(-1)
        output_tensor = transpose((lambda_b * vera_B) @ (lambda_d * vera_A), self.fan_in_fan_out)

        if cast_to_fp32:
            output_tensor = output_tensor.to(dtype=dtype)

            # cast back the weights
            # TODO: why?
            self.vera_lambda_d[adapter].data = lambda_d.to(dtype)
            self.vera_lambda_b[adapter].data = lambda_b.to(dtype)

        return output_tensor

    # we use a forward hook to populate vera_A and vera_B
    # this indirectly enforces the constraint of only supporting a single vera shape for now.
    def forward(self, x: torch.Tensor, vera_A: torch.Tensor, vera_B: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        previous_dtype = x.dtype

        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            for active_adapter in self.active_adapters:
                if active_adapter not in self.vera_lambda_d.keys():
                    continue

                lambda_d = self.vera_lambda_d[active_adapter]
                lambda_b = self.vera_lambda_b[active_adapter]

                dropout = self.vera_dropout[active_adapter]
                x = x.to(lambda_d.dtype)
                result += lambda_b * F.linear(lambda_d * F.linear(dropout(x), vera_A), vera_B)

        result = result.to(previous_dtype)
        return result


class Embedding(nn.Embedding, VeraLayer):
    # Vera implemented in a Embedding layer
    def __init__(
        self,
        base_layer,
        adapter_name: str,
        r: int = 0,
        vera_dropout: float = 0.0,
        d_initial: float = 1.0,
        **kwargs,
    ) -> None:
        init_vera_weights = kwargs.pop("init_vera_weights", True)
        VeraLayer.__init__(self, base_layer, **kwargs)
        self.update_layer_embedding(adapter_name, r, vera_dropout, init_vera_weights, d_initial=d_initial)

    def merge(self, vera_A: torch.Tensor, vera_B: torch.Tensor, safe_merge: bool = False) -> None:
        """
        Merge the active adapter weights into the base weights

        Args:
            safe_merge (`bool`, *optional*):
                If True, the merge operation will be performed in a copy of the original weights and check for NaNs
                before merging the weights. This is useful if you want to check if the merge operation will produce
                NaNs. Defaults to `False`.
        """
        if self.merged:
            warnings.warn(
                f"Already following adapters were merged {','.join(self.merged_adapters)}. "
                f"You are now additionally merging {','.join(self.active_adapters)}."
            )
        for active_adapter in self.active_adapters:
            if active_adapter in self.vera_lambda_d.keys():
                base_layer = self.get_base_layer()
                if safe_merge:
                    # Note that safe_merge will be slower than the normal merge
                    # because of the copy operation.
                    orig_weights = base_layer.weight.data.copy()
                    orig_weights += self.get_delta_weight(active_adapter, vera_A, vera_B)

                    if not torch.isfinite(orig_weights).all():
                        raise ValueError(
                            f"NaNs detected in the merged weights. The adapter {active_adapter} seems to be broken"
                        )

                    base_layer.weight.data = orig_weights
                else:
                    base_layer.weight.data += self.get_delta_weight(active_adapter, vera_A, vera_B)
                self.merged_adapters.append(active_adapter)

    def unmerge(self, vera_A: torch.Tensor, vera_B: torch.Tensor) -> None:
        if not self.merged:
            warnings.warn("Already unmerged. Nothing to do.")
            return
        while len(self.merged_adapters) > 0:
            active_adapter = self.merged_adapters.pop()
            if active_adapter in self.vera_lambda_d.keys():
                self.weight.data -= self.get_delta_weight(active_adapter, vera_A, vera_B)

    def get_delta_weight(self, adapter, vera_A: torch.Tensor, vera_B: torch.Tensor) -> torch.Tensor:
        """
        Compute the delta weight for the given adapter.

        Args:
            adapter (str):
                The name of the adapter for which the delta weight should be computed.
        """
        device = vera_A.device
        dtype = vera_A.dtype

        # In case users wants to merge the adapter weights that are in
        # float16 while being on CPU, we need to cast the weights to float32, perform the merge and then cast back to
        # float16 because the `@` and matmul operation in general is not supported in torch + cpu + fp16.
        cast_to_fp32 = device.type == "cpu" and dtype == torch.float16

        lambda_d = self.vera_lambda_d[adapter]
        lambda_b = self.vera_lambda_b[adapter]

        if cast_to_fp32:
            vera_A = vera_A.float()
            vera_B = vera_B.float()
            lambda_d = lambda_d.float()
            lambda_b = lambda_b.float()

        lambda_b = lambda_b.unsqueeze(-1)
        lambda_d = lambda_d.unsqueeze(-1)
        output_tensor = transpose((lambda_b * vera_B) @ (lambda_d * vera_A), True)

        if cast_to_fp32:
            output_tensor = output_tensor.to(dtype=dtype)

            # cast back the weights
            self.vera_lambda_d[adapter].data = lambda_d.to(dtype)
            self.vera_lambda_b[adapter].data = lambda_b.to(dtype)

        return output_tensor

    def _embed(self, input: torch.Tensor, weight: Optional[torch.Tensor] = None) -> torch.Tensor:
        weight = self.weight if weight is None else weight
        return F.embedding(
            input,
            weight,
            padding_idx=self.padding_idx,
            max_norm=self.max_norm,
            norm_type=self.norm_type,
            scale_grad_by_freq=self.scale_grad_by_freq,
            sparse=self.sparse,
        )

    def forward(self, x: torch.Tensor, vera_A: torch.Tensor, vera_B: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        if self.disable_adapters:
            if self.merged:
                self.unmerge()
            result = self.base_layer(x, *args, **kwargs)
        elif self.merged:
            result = self.base_layer(x, *args, **kwargs)
        else:
            result = self.base_layer(x, *args, **kwargs)
            for active_adapter in self.active_adapters:
                if active_adapter not in self.vera_lambda_d:
                    continue
                lambda_d = self.vera_lambda_d[active_adapter]
                lambda_b = self.vera_lambda_b[active_adapter]

                after_A = lambda_d * self._embed(x, vera_A.T)
                result += lambda_b * (after_A @ vera_B.T)

        return result
