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

# Reference paper: https://arxiv.org/abs/2404.02948

from __future__ import annotations


import os
import torch
from copy import deepcopy
from safetensors import safe_open
from safetensors.torch import save_file
import json


def save_pissa_initialization(peft_model, pissa_initial_dir, pissa_residual_dir = None):
    # No need for SVD when we load the PiSSA and residual model saved locally.
    peft_model.peft_config["default"].init_lora_weights = True
    # Save PiSSA adapter.
    peft_model.save_pretrained(pissa_initial_dir)

    if pissa_residual_dir is not None:
        residual_model = deepcopy(peft_model).to('cpu')
        residual_model = residual_model.unload()
        # Save the residual model and the tokenizer.
        residual_model.save_pretrained(pissa_residual_dir)
        del residual_model


# A PiSSA of rank r can be equivalently represented by a LoRA of rank 2r.
# The advantage of PiSSA lies in the training phase. Upon completion of training, when sharing with others, it is recommended to convert PiSSA into LoRA.
# LoRA does not modify the parameters of the base model during using.
# When multiple converted-LoRAs are needed simultaneously, each adapter works independently without interference, allowing for the adapters to be freely deleted or added.


def pissa_to_lora(
    init_path,
    finetuned_path,
    output_path,
    device="cpu",
    tensors_name="adapter_model.safetensors",
    config_name="adapter_config.json",
):
    tensors_init = {}

    with safe_open(os.path.join(init_path, tensors_name), framework="pt", device=device) as f:
        for k in f.keys():
            tensors_init[k] = f.get_tensor(k)

    tensors_finetune = {}
    with safe_open(os.path.join(finetuned_path, tensors_name), framework="pt", device=device) as f:
        for k in f.keys():
            tensors_finetune[k] = f.get_tensor(k)
    tensors_delta_w = {}
    for name in tensors_init.keys():
        ## W = W^{res} + A_0 \times B_0,
        ## W + \Delta W = W^{res} + A \times B,
        ## \Delta W = A \times B - A_0 \times B_0 = [A | A_0] \times [B | B_0]^T = A'B'.
        tensors_delta_w[name] = (
            torch.cat([tensors_finetune[name], tensors_init[name]], dim=0)
            if "lora_A" in name
            else torch.cat([tensors_finetune[name], -tensors_init[name]], dim=1)
        )

    if not os.path.exists(output_path):
        os.mkdir(output_path)
    save_file(tensors_delta_w, os.path.join(output_path, tensors_name))

    with open(os.path.join(init_path, config_name)) as f:
        adapter_config = json.load(f)
    adapter_config["init_lora_weights"] = True
    adapter_config["r"] *= 2
    adapter_config["lora_alpha"] *= 2
    with open(os.path.join(output_path, config_name), "w") as f:
        json.dump(adapter_config, f)
