# Copyright 2024-present the HuggingFace Inc. team.
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

# This test file is for tests specific to VeRA, since VeRA has some specific challenges due to the shared weights.

import os

import pytest
import torch
from torch import nn

from peft import PeftModel, VBLoRAConfig, get_peft_model


class MLP(nn.Module):
    def __init__(self, bias=True):
        super().__init__()
        self.relu = nn.ReLU()
        self.lin0 = nn.Linear(10, 20, bias=bias)
        self.lin1 = nn.Linear(20, 20, bias=bias)  # lin1 and lin2 have same shape
        self.lin2 = nn.Linear(20, 20, bias=bias)
        self.lin3 = nn.Linear(20, 2, bias=bias)
        self.sm = nn.LogSoftmax(dim=-1)

    def forward(self, X):
        X = self.lin0(X)
        X = self.relu(X)
        X = self.lin1(X)
        X = self.relu(X)
        X = self.lin2(X)
        X = self.relu(X)
        X = self.lin3(X)
        X = self.sm(X)
        return X


class TestVBLoRA:
    @pytest.fixture
    def mlp(self):
        model = MLP()
        return model

    def test_vblora_parameters(self, mlp):
        config = VBLoRAConfig(target_modules=["lin0", "lin1", "lin3"], vector_length=2, num_vectors=10)
        mlp_vblora = get_peft_model(mlp, config)

        vector_bank = mlp_vblora.vblora_vector_bank["default"]

        vblora_lin0_logits_B = mlp_vblora.lin0.vblora_logits_B["default"]
        assert vblora_lin0_logits_B.shape == (mlp.lin0.out_features // 2, config.r, 10)

        vblora_lin1_logits_A = mlp_vblora.lin1.vblora_logits_A["default"]
        assert vblora_lin1_logits_A.shape == (config.r, mlp.lin1.in_features // 2, 10)

        vblora_lin3_logits_A = mlp_vblora.lin3.vblora_logits_A["default"]
        assert vblora_lin3_logits_A.shape == (config.r, mlp.lin3.in_features // 2, 10)

        assert vector_bank.shape == (10, 2)

        assert (
            mlp_vblora.lin0.vblora_vector_bank["default"].data_ptr()
            == mlp_vblora.lin3.vblora_vector_bank["default"].data_ptr()
        )
        assert mlp_vblora.lin1.vblora_vector_bank["default"].data_ptr() == vector_bank.data_ptr()

        # should not raise
        input = torch.randn(5, 10)
        mlp_vblora(input)

    def test_save_load_save(self, mlp, tmp_path):
        config = VBLoRAConfig(target_modules=["lin0", "lin1", "lin3"], vector_length=2, num_vectors=10)
        mlp_vblora = get_peft_model(mlp, config)
        save_path = tmp_path / "vblora"
        mlp_vblora.save_pretrained(save_path)

        assert os.path.exists(save_path / "adapter_config.json")

        mlp_vblora_loaded = PeftModel.from_pretrained(mlp, save_path)

        input = torch.randn(5, 10)
        output = mlp_vblora(input)
        output_loaded = mlp_vblora_loaded(input)
        assert torch.allclose(output, output_loaded, atol=1e-3, rtol=1e-3)

    def test_save_load_save_topk_only(self, mlp, tmp_path):
        config = VBLoRAConfig(
            target_modules=["lin0", "lin1", "lin3"], topk=2, vector_length=2, num_vectors=10, save_topk_weights=True
        )
        mlp_vblora = get_peft_model(mlp, config)
        save_path = tmp_path / "vblora"
        mlp_vblora.save_pretrained(save_path)
        print("save_path", save_path)
        assert os.path.exists(save_path / "adapter_config.json")

        mlp_vblora_loaded = PeftModel.from_pretrained(mlp, save_path)

        input = torch.randn(5, 10)
        output = mlp_vblora(input)
        output_loaded = mlp_vblora_loaded(input)
        assert torch.allclose(output, output_loaded, atol=1e-3, rtol=1e-3)

    @pytest.mark.parametrize("dtype", [torch.float32, torch.float16, torch.bfloat16])
    def test_vblora_dtypes(self, mlp, dtype):
        if (dtype == torch.bfloat16) and not (torch.cuda.is_available() and torch.cuda.is_bf16_supported()):
            pytest.skip("bfloat16 not supported on this system, skipping the test")

        config = VBLoRAConfig(
            target_modules=["lin0", "lin1", "lin3"], vector_length=2, num_vectors=10, save_topk_weights=True
        )
        mlp_vblora = get_peft_model(mlp.to(dtype), config)
        inputs = torch.randn(5, 10).to(dtype)
        output = mlp_vblora(inputs)  # should not raise
        assert output.dtype == dtype
