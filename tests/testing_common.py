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
from collections import OrderedDict

from peft import (
    LoraConfig,
    PrefixTuningConfig,
    PromptEncoderConfig,
    PromptTuningConfig,
)


CONFIG_CLASSES = (
    LoraConfig,
    PrefixTuningConfig,
    PromptEncoderConfig,
    PromptTuningConfig,
)
CONFIG_TESTING_KWARGS = (
    {
        "r": 8,
        "lora_alpha": 32,
        "target_modules": None,
        "lora_dropout": 0.05,
        "bias": "none",
        "task_type": "CAUSAL_LM",
    },
    {
        "num_virtual_tokens": 10,
        "task_type": "CAUSAL_LM",
    },
    {
        "num_virtual_tokens": 10,
        "encoder_hidden_size": 32,
        "task_type": "CAUSAL_LM",
    },
    {
        "num_virtual_tokens": 10,
        "task_type": "CAUSAL_LM",
    },
)

CLASSES_MAPPING = {
    "lora": (LoraConfig, CONFIG_TESTING_KWARGS[0]),
    "prefix_tuning": (PrefixTuningConfig, CONFIG_TESTING_KWARGS[1]),
    "prompt_encoder": (PromptEncoderConfig, CONFIG_TESTING_KWARGS[2]),
    "prompt_tuning": (PromptTuningConfig, CONFIG_TESTING_KWARGS[3]),
}


# Adapted from https://github.com/huggingface/transformers/blob/48327c57182fdade7f7797d1eaad2d166de5c55b/src/transformers/activations.py#LL166C7-L166C22
class ClassInstantier(OrderedDict):
    def __getitem__(self, key, *args, **kwargs):
        # check if any of the kwargs is inside the config class kwargs
        if any([kwarg in self[key][1] for kwarg in kwargs]):
            new_config_kwargs = self[key][1].copy()
            new_config_kwargs.update(kwargs)
            return (self[key][0], new_config_kwargs)

        return super().__getitem__(key, *args, **kwargs)

    def get_grid_parameters(self, grid_parameters):
        r"""
        Returns a list of all possible combinations of the parameters in the config classes.
        """
        generated_tests = []
        model_list = grid_parameters["model_ids"]

        for model_id in model_list:
            for key, value in self.items():
                peft_methods = [value[1].copy()]

                if "{}_kwargs".format(key) in grid_parameters:
                    for current_key, current_value in grid_parameters[f"{key}_kwargs"].items():
                        for kwarg in current_value:
                            peft_methods.append(value[1].copy().update({current_key: kwarg}))

                for peft_method in peft_methods:
                    generated_tests.append((f"test_{model_id}_{key}", model_id, value[0], peft_method))

        return generated_tests


PeftTestConfigManager = ClassInstantier(CLASSES_MAPPING)
