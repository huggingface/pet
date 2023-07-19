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

import os
import tempfile

import torch
from torch.testing import assert_close

from peft.mapping import get_peft_model
from peft.peft_model import PeftModel
from peft.tuners.multitask_prompt_tuning import MultitaskPromptTuningConfig
from peft.utils.other import prepare_model_for_int8_training
from peft.utils.save_and_load import get_peft_model_state_dict
from tests.test_adaption_prompt import AdaptionPromptTester
from transformers import LlamaForCausalLM


class MultiTaskPromptTuningTester(AdaptionPromptTester):
    """
    Tests for the MultiTaskPromptTuning model.

    Some of these tests were adapted from `test_peft_model.py` (which has been refactored since), but since we haven't
    checked in the test checkpoints for Llama into `hf-internal-testing`, we separate them for now.
    """

    @classmethod
    def _create_multitask_prompt_tuning_config(cls) -> MultitaskPromptTuningConfig:
        return MultitaskPromptTuningConfig(
            task_type="CAUSAL_LM",
            num_virtual_tokens=50,
            num_tasks=3,
            prompt_tuning_init_text="classify the following into either positive or negative, or entailment, neutral or contradiction:",
        )

    def test_prepare_for_training(self) -> None:
        model = LlamaForCausalLM(self._create_test_llama_config())
        model = get_peft_model(model, self._create_multitask_prompt_tuning_config())
        model = model.to(self.torch_device)

        dummy_input = torch.LongTensor([[1, 1, 1]]).to(self.torch_device)
        dummy_output = model.get_input_embeddings()(dummy_input)

        self.assertTrue(not dummy_output.requires_grad)

    def test_prepare_for_int8_training(self) -> None:
        model = LlamaForCausalLM(self._create_test_llama_config())
        model = prepare_model_for_int8_training(model)
        model = model.to(self.torch_device)

        for param in model.parameters():
            self.assertTrue(not param.requires_grad)

        model = get_peft_model(model, self._create_multitask_prompt_tuning_config())

        # For backward compatibility
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
        else:

            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)

            model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

        dummy_input = torch.LongTensor([[1, 1, 1]]).to(self.torch_device)
        dummy_output = model.get_input_embeddings()(dummy_input)

        self.assertTrue(dummy_output.requires_grad)

    def test_save_pretrained(self) -> None:
        seed = 420
        torch.manual_seed(seed)
        model = LlamaForCausalLM(self._create_test_llama_config())
        model = get_peft_model(model, self._create_multitask_prompt_tuning_config())
        model = model.to(self.torch_device)

        with tempfile.TemporaryDirectory() as tmp_dirname:
            model.save_pretrained(tmp_dirname)

            torch.manual_seed(seed)
            model_from_pretrained = LlamaForCausalLM(self._create_test_llama_config())
            model_from_pretrained = PeftModel.from_pretrained(model_from_pretrained, tmp_dirname)

            # check if the state dicts are equal
            state_dict = get_peft_model_state_dict(model)
            state_dict_from_pretrained = get_peft_model_state_dict(model_from_pretrained)

            # check if same keys
            self.assertEqual(state_dict.keys(), state_dict_from_pretrained.keys())

            # Check that the number of saved parameters is 4 -- 2 layers of (tokens and gate).
            self.assertEqual(len(list(state_dict.keys())), 4)

            # check if tensors equal
            for key in state_dict.keys():
                self.assertTrue(
                    torch.allclose(
                        state_dict[key].to(self.torch_device), state_dict_from_pretrained[key].to(self.torch_device)
                    )
                )

            # check if `adapter_model.bin` is present
            self.assertTrue(os.path.exists(os.path.join(tmp_dirname, "adapter_model.bin")))

            # check if `adapter_config.json` is present
            self.assertTrue(os.path.exists(os.path.join(tmp_dirname, "adapter_config.json")))

            # check if `pytorch_model.bin` is not present
            self.assertFalse(os.path.exists(os.path.join(tmp_dirname, "pytorch_model.bin")))

            # check if `config.json` is not present
            self.assertFalse(os.path.exists(os.path.join(tmp_dirname, "config.json")))

    def test_generate(self) -> None:
        model = LlamaForCausalLM(self._create_test_llama_config())
        model = get_peft_model(model, self._create_multitask_prompt_tuning_config())
        model = model.to(self.torch_device)

        input_ids = torch.LongTensor([[1, 1, 1], [2, 1, 2]]).to(self.torch_device)
        attention_mask = torch.LongTensor([[1, 1, 1], [1, 0, 1]]).to(self.torch_device)

        # check if `generate` works
        _ = model.generate(input_ids=input_ids, attention_mask=attention_mask)

        with self.assertRaises(TypeError):
            # check if `generate` raises an error if no positional arguments are passed
            _ = model.generate(input_ids, attention_mask=attention_mask)

    def test_sequence_adapter_ops(self) -> None:
        """Test sequence of adapter operations."""
        # Test input data.
        input_ids = torch.LongTensor([[1, 1, 1], [2, 1, 2]]).to(self.torch_device)
        target_ids = torch.LongTensor([[0, 0, 0], [0, 0, 0]]).to(self.torch_device)
        attention_mask = torch.LongTensor([[1, 1, 1], [1, 0, 1]]).to(self.torch_device)

        # Create original llama model.
        original = LlamaForCausalLM(self._create_test_llama_config())
        original = original.to(self.torch_device)
        original_before = original(input_ids=input_ids, attention_mask=attention_mask)

        # Get AdaptionPrompt model.
        mpt = get_peft_model(original, self._create_multitask_prompt_tuning_config())
        mpt = mpt.to(self.torch_device)
        default_before = mpt(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)

        # Test zero-init: The logits should be exactly the same.
        assert_close(original_before.logits, default_before.logits, rtol=0, atol=0)

        # Single fine-tuning step on "default" adapter.
        optimizer = torch.optim.SGD(mpt.parameters(), lr=1)
        optimizer.zero_grad()
        default_before.loss.backward()
        optimizer.step()

        # Test that the output changed.
        default_after = mpt(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        self.assertFalse(torch.allclose(default_before.logits, default_after.logits))

        with mpt.disable_adapter():
            # Test that the output is the same as the original ouput.
            default_disabled = mpt(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
            assert_close(original_before.logits, default_disabled.logits, rtol=0, atol=0)

        # Add new adapter 1.
        mpt.add_adapter("adapter 1", AdaptionPromptConfig(adapter_layers=3, adapter_len=8, task_type="CAUSAL_LM"))
        # Test zero-init
        adapter_1_before = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        assert_close(original_before.logits, adapter_1_before.logits, rtol=0, atol=0)

        # Single fine-tuning step on adapter 1.
        optimizer = torch.optim.SGD(adapted.parameters(), lr=1)
        optimizer.zero_grad()
        adapter_1_before.loss.backward()
        optimizer.step()

        # Test that adapter 1 output changed.
        adapter_1_after = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        self.assertFalse(torch.allclose(adapter_1_before.logits, adapter_1_after.logits))
        self.assertFalse(torch.allclose(original_before.logits, adapter_1_after.logits))
        self.assertFalse(torch.allclose(default_after.logits, adapter_1_after.logits))

        with adapted.disable_adapter():
            # Test that the output is the same as the original output.
            adapter_1_disabled = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
            assert_close(original_before.logits, adapter_1_disabled.logits, rtol=0, atol=0)

        # Set adapter back to default.
        adapted.set_adapter("default")

        # Test that the output is the same as the default output after training.
        default_after_set = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        assert_close(default_after.logits, default_after_set.logits, rtol=0, atol=0)
        self.assertFalse(torch.allclose(original_before.logits, default_after_set.logits))
        self.assertFalse(torch.allclose(adapter_1_after.logits, default_after_set.logits))

    def test_add_and_set_while_disabled(self):
        """Test that adding and setting adapters while disabled works as intended."""
        # Test input data.
        input_ids = torch.LongTensor([[1, 1, 1], [2, 1, 2]]).to(self.torch_device)
        target_ids = torch.LongTensor([[0, 0, 0], [0, 0, 0]]).to(self.torch_device)
        attention_mask = torch.LongTensor([[1, 1, 1], [1, 0, 1]]).to(self.torch_device)

        # Create original llama model.
        original = LlamaForCausalLM(self._create_test_llama_config())
        original = original.to(self.torch_device)
        original_before = original(input_ids=input_ids, attention_mask=attention_mask)

        # Get AdaptionPrompt model.
        adapted = get_peft_model(
            original, AdaptionPromptConfig(adapter_layers=2, adapter_len=4, task_type="CAUSAL_LM")
        )
        adapted = adapted.to(self.torch_device)

        with adapted.disable_adapter():
            adapted.add_adapter(
                "adapter 1", AdaptionPromptConfig(adapter_layers=3, adapter_len=8, task_type="CAUSAL_LM")
            )

        # Test that the output is the same as the original output.
        adapter_1_before = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        assert_close(original_before.logits, adapter_1_before.logits, rtol=0, atol=0)

        # Single fine-tuning step on adapter 1.
        optimizer = torch.optim.SGD(adapted.parameters(), lr=1)
        optimizer.zero_grad()
        adapter_1_before.loss.backward()
        optimizer.step()

        # Test that adapter 1 output changed.
        adapter_1_after = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        self.assertFalse(torch.allclose(original_before.logits, adapter_1_after.logits))

        adapted.set_adapter("default")
        with adapted.disable_adapter():
            adapted.set_adapter("adapter 1")

        # Test that adapter 1 is active again.
        adapter_1_after_set = adapted(input_ids=input_ids, attention_mask=attention_mask, labels=target_ids)
        assert_close(adapter_1_after.logits, adapter_1_after_set.logits, rtol=0, atol=0)

    def test_use_cache(self) -> None:
        """Test that MultiTaskPromptTuning works when Llama config use_cache=True."""
        input_ids = torch.LongTensor([[1, 1, 1], [2, 1, 2]]).to(self.torch_device)
        original = LlamaForCausalLM(self._create_test_llama_config())
        mpt = get_peft_model(original, self._create_multitask_prompt_tuning_config())
        mpt = mpt.to(self.torch_device)
        expected = mpt.generate(input_ids=input_ids, max_length=8)

        # Set use_cache = True and generate output again.
        mpt.base_model.config.use_cache = True
        actual = mpt.generate(input_ids=input_ids, max_length=8)
        assert_close(expected, actual, rtol=0, atol=0)

    def test_bf16_inference(self) -> None:
        """Test that MultiTaskPromptTuning works when Llama using a half-precision model."""
        input_ids = torch.LongTensor([[1, 1, 1], [2, 1, 2]]).to(self.torch_device)
        original = LlamaForCausalLM.from_pretrained(
            "trl-internal-testing/tiny-random-LlamaForCausalLM", torch_dtype=torch.bfloat16
        )
        mpt = get_peft_model(original, self._create_multitask_prompt_tuning_config())
        mpt = mpt.to(self.torch_device)
        _ = mpt.generate(input_ids=input_ids)
