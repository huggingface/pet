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

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Optional, Union

import torch
from torch import nn
from transformers.utils import PushToHubMixin

from peft.tuners.mixed import COMPATIBLE_TUNER_TYPES

from .config import PeftConfig
from .tuners import (
    AdaLoraModel,
    IA3Model,
    LoHaModel,
    LoKrModel,
    LoraModel,
    MixedModel,
)
from .utils import (
    PeftType,
    _set_adapter,
    _set_trainable,
)


PEFT_TYPE_TO_MODEL_MAPPING = {
    PeftType.LORA: LoraModel,
    PeftType.LOHA: LoHaModel,
    PeftType.LOKR: LoKrModel,
    PeftType.ADALORA: AdaLoraModel,
    PeftType.IA3: IA3Model,
}


def _prepare_model_for_gradient_checkpointing(model: nn.Module) -> None:
    r"""
    Prepares the model for gradient checkpointing if necessary
    """
    # Note: same as PeftModel._prepare_model_for_gradient_checkpointing
    if not getattr(model, "is_gradient_checkpointing", True):
        return model

    if not (
        getattr(model, "is_loaded_in_8bit", False)
        or getattr(model, "is_loaded_in_4bit", False)
        or getattr(model, "is_quantized", False)
    ):
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
        elif hasattr(model, "get_input_embeddings"):

            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)

            model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)


def _check_config_compatible(peft_config: PeftConfig) -> None:
    if peft_config.peft_type not in COMPATIBLE_TUNER_TYPES:
        raise ValueError(
            f"The provided `peft_type` '{peft_config.peft_type.value}' is not compatible with the `PeftMixedModel`. "
            f"Compatible types are: {COMPATIBLE_TUNER_TYPES}"
        )


class PeftMixedModel(PushToHubMixin, torch.nn.Module):
    """
    Peft model for mixing different types of adapters.

    This class currently does not support saving and loading. Instead, it is assumed that the adapters are already
    trained and loading the model requires a script to be run each time.

    Currently, the main purpose of mixed adapter types is to combine trained adapters for inference. Although it is
    technically possible to train a mixed adapter model, this has not been tested and is not recommended.

    Note: This class should usually not be initialized directly. Instead, use `get_peft_model` with the argument
    `mixed=True`.

    Below is an example that shows how to load a mixed model with two different types of adapters.

    ```py
    >>> from peft import get_peft_model

    >>> base_model = ...  # load the base model, e.g. from transformers
    >>> config0 = PeftConfig.from_pretrained(...)  # load first adapter, e.g. LoRA
    >>> peft_model = get_peft_model(base_model, config0, adapter_name="default", mixed=True)
    >>> config1 = PeftConfig.from_pretrained(...)  # load second adapter, e.g. LoHa
    >>> peft_model.add_adapter(config1, "loha")
    >>> peft_model.set_adapter(["default", "loha"])
    ```

    Tips:

    - Not all adapter types can be combined. See `peft.tuners.mixed.COMPATIBLE_TUNER_TYPES` for a list of compatible
      types. An error will be raised if you are trying to combine incompatible adapter types.
    - It is possible to mix multiple adapters of the same type. This can be useful to combine adapters with very
      different configs.
    - If you want to combine a lot of different adapters, it is most performant to add the same types of adapters
      consecutively. E.g., add LoRA1, LoRA2, LoHa1, LoHa2 in this order, instead of LoRA1, LoHa1, LoRA2, LoHa2. As long
      as the adapters are commutative, the order does not matter for the final result.

    Args:
        model (`torch.nn.Module`):
            The model to be tuned.
        config (`PeftConfig`):
            The config of the model to be tuned. The adapter type must be compatible.
        adapter_name (`str`, `optional`, defaults to `"default"`):
            The name of the first adapter.
    """

    def __init__(self, model: nn.Module, peft_config: PeftConfig, adapter_name: str = "default") -> None:
        super().__init__()
        _check_config_compatible(peft_config)
        _prepare_model_for_gradient_checkpointing(model)
        self.modules_to_save = None
        self.base_model = MixedModel(model, {adapter_name: peft_config}, adapter_name)
        self.set_modules_to_save(peft_config, adapter_name)

        self.config = getattr(model, "config", {"model_type": "custom"})

        # the `pretraining_tp` is set for some models to simulate Tensor Parallelism during inference to avoid
        # numerical differences, https://github.com/pytorch/pytorch/issues/76232 - to avoid any unexpected
        # behavior we disable that in this line.
        if hasattr(self.base_model, "config") and hasattr(self.base_model.config, "pretraining_tp"):
            self.base_model.config.pretraining_tp = 1

    @property
    def peft_config(self) -> dict[str, PeftConfig]:
        return self.base_model.peft_config

    @property
    def active_adapter(self) -> str:
        return self.base_model.active_adapter

    @property
    def active_adapters(self) -> list[str]:
        return self.base_model.active_adapters

    def get_nb_trainable_parameters(self):
        r"""
        Returns the number of trainable parameters and number of all parameters in the model.
        """
        # note: same as PeftModel.get_nb_trainable_parameters
        trainable_params = 0
        all_param = 0
        for _, param in self.named_parameters():
            num_params = param.numel()
            # if using DS Zero 3 and the weights are initialized empty
            if num_params == 0 and hasattr(param, "ds_numel"):
                num_params = param.ds_numel

            # Due to the design of 4bit linear layers from bitsandbytes
            # one needs to multiply the number of parameters by 2 to get
            # the correct number of parameters
            if param.__class__.__name__ == "Params4bit":
                num_params = num_params * 2

            all_param += num_params
            if param.requires_grad:
                trainable_params += num_params

        return trainable_params, all_param

    def print_trainable_parameters(self):
        """
        Prints the number of trainable parameters in the model.
        """
        # note: same as PeftModel.print_trainable_parameters
        trainable_params, all_param = self.get_nb_trainable_parameters()

        print(
            f"trainable params: {trainable_params:,d} || "
            f"all params: {all_param:,d} || "
            f"trainable%: {100 * trainable_params / all_param:.4f}"
        )

    def forward(self, *args: Any, **kwargs: Any):
        """
        Forward pass of the model.
        """
        return self.base_model(*args, **kwargs)

    def generate(self, *args: Any, **kwargs: Any):
        """
        Generate output.
        """
        return self.base_model.generate(*args, **kwargs)

    @contextmanager
    def disable_adapter(self):
        """
        Disables the adapter module.
        """
        try:
            self.base_model.disable_adapter_layers()
            yield
        finally:
            self.base_model.enable_adapter_layers()

    def add_adapter(self, adapter_name: str, peft_config: PeftConfig):
        _check_config_compatible(peft_config)

        try:
            self.peft_config[adapter_name] = peft_config
            self.base_model.inject_adapter(self, adapter_name)
        except Exception:  # somthing went wrong, roll back
            if adapter_name in self.peft_config:
                del self.peft_config[adapter_name]
            raise

        self.set_modules_to_save(peft_config, adapter_name)

    def set_modules_to_save(self, peft_config: PeftConfig, adapter_name: str) -> None:
        if (modules_to_save := getattr(peft_config, "modules_to_save", None)) is None:
            return

        if self.modules_to_save is None:
            self.modules_to_save = set(modules_to_save)
        else:
            self.modules_to_save.update(modules_to_save)
        _set_trainable(self, adapter_name)

    def set_adapter(self, adapter_name: Union[str, list[str]]) -> None:
        """
        Sets the active adapter.
        """
        if isinstance(adapter_name, str):
            adapter_name = [adapter_name]

        mismatched = set(adapter_name) - set(self.peft_config.keys())
        if mismatched:
            raise ValueError(
                f"Adapter(s) {sorted(mismatched)} not found, available adapters: {sorted(self.peft_config.keys())}"
            )

        self.base_model.set_adapter(adapter_name)
        _set_adapter(self, adapter_name)

    def delete_adapter(self, adapter_name: Union[str, list[str]]) -> None:
        if isinstance(adapter_name, str):
            adapter_name = [adapter_name]

        mismatched = set(adapter_name) - set(self.peft_config.keys())
        if mismatched:
            raise ValueError(
                f"Adapter(s) {sorted(mismatched)} not found, available adapters: {sorted(self.peft_config.keys())}"
            )

        self.base_model.delete_adapter(adapter_name)

    def merge_and_unload(self, *args: Any, **kwargs: Any):
        r"""
        This method merges the adapter layers into the base model. This is needed if someone wants to use the base
        model as a standalone model.

        Args:
            progressbar (`bool`):
                whether to show a progressbar indicating the unload and merge process
            safe_merge (`bool`):
                whether to activate the safe merging check to check if there is any potential Nan in the adapter
                weights
            adapter_names (`List[str]`, *optional*):
                The list of adapter names that should be merged. If None, all active adapters will be merged. Defaults
                to `None`.
        """
        return self.base_model.merge_and_unload(*args, **kwargs)

    def unload(self, *args: Any, **kwargs: Any):
        """
        Gets back the base model by removing all the adapter modules without merging. This gives back the original base
        model.
        """
        return self.base_model.unload(*args, **kwargs)

    def load_adapter(
        self, model_id: str, adapter_name: str, is_trainable: bool = False, **kwargs: Any
    ) -> tuple[list[str], list[str]]:
        raise NotImplementedError(f"Loading is not supported for {self.__class__.__name__} (yet).")

    def create_or_update_model_card(self, output_dir: str):
        raise NotImplementedError(f"Model card creation is not supported for {self.__class__.__name__} (yet).")

    def save_pretrained(
        self,
        save_directory: str,
        safe_serialization: bool = False,
        selected_adapters: Optional[list[str]] = None,
        **kwargs: Any,
    ):
        raise NotImplementedError(f"Saving is not supported for {self.__class__.__name__} (yet).")

    @classmethod
    def from_pretrained(
        cls,
        model: nn.Module,
        model_id: str | os.PathLike,
        adapter_name: str = "default",
        is_trainable: bool = False,
        config: Optional[PeftConfig] = None,
        **kwargs: Any,
    ):
        raise NotImplementedError(f"Loading is not supported for {cls.__name__} (yet).")
