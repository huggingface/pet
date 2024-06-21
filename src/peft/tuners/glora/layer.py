import math
import random
import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F

from peft.tuners.tuners_utils import BaseTunerLayer


class GLoraLayer(BaseTunerLayer):
    def __init__(
        self, base_layer: nn.Module, in_features: int, out_features: int, r: int, adapter_name: str, **kwargs
    ):
        super().__init__()
        self.base_layer = base_layer
        self.r = {}
        self.r[adapter_name] = r
        self.in_features = in_features
        self.out_features = out_features

        # Initialize learnable parameters
        self.glora_Ad, self.glora_Au = self.make_param((out_features, in_features), f"LoRA_{r}")
        self.glora_Bd, self.glora_Bu = self.make_param((out_features, in_features), f"LoRA_{r}")
        self.glora_Cd, self.glora_Cu = self.make_param((in_features, 1), f"LoRA_{r}")
        self.glora_D = nn.Parameter(torch.zeros(out_features))
        self.glora_E = nn.Parameter(torch.zeros(out_features))

        # Generate configurations
        config_A_B = [f"LoRA_{r}", "vector", "constant", "none"]
        config_C = [f"LoRA_{r}", "vector", "none"]
        config_D_E = ["constant", "none", "vector"]
        self.configs = [
            {"A": A, "B": B, "C": C, "D": D, "E": E}
            for A in config_A_B
            for B in config_A_B
            for C in config_C
            for D in config_D_E
            for E in config_D_E
        ]



    def make_param(self, shape, config=None):
        if "LoRA" in config:
            out_feature = shape[0]
            in_feature = shape[1]
            try:
                rank = int(config.split("_")[1])
            except ValueError:
                rank = 4
            return nn.Parameter(torch.zeros(out_feature, rank)), nn.Parameter(torch.zeros(rank, in_feature))
        return nn.Parameter(torch.zeros(*shape))

    def __repr__(self) -> str:
        rep = super().__repr__()
        return "glora." + rep


class Linear(nn.Module, GLoraLayer):
    # GLora implemented in a dense layer
    def __init__(
        self,
        base_layer: nn.Module,
        adapter_name: str,
        in_features: int,
        out_features: int,
        r: int = 0,
        bias: bool = True,
        **kwargs,
    ) -> None:
        super().__init__()
        GLoraLayer.__init__(self, base_layer, in_features, out_features, r, adapter_name, **kwargs)
        self.base_layer = base_layer
        self.in_features = in_features
        self.out_features = out_features
        self.r = r
        #self.bias = bias
        self._active_adapter = adapter_name
        self.eval_config = None
        self.to(self.weight.device)
        self.weight.requires_grad = False
        # Initialize support tensors
        self.glora_Ad = nn.Parameter(torch.randn(out_features, r))
        self.glora_Au = nn.Parameter(torch.randn(r, in_features))
        self.glora_Bd = nn.Parameter(torch.randn(out_features, r))
        self.glora_Bu = nn.Parameter(torch.randn(r, in_features))
        self.glora_Cd = nn.Parameter(torch.randn(out_features, r))
        self.glora_Cu = nn.Parameter(torch.randn(r, 1))
        self.glora_D = nn.Parameter(torch.randn(out_features, 1))
        self.glora_E = nn.Parameter(torch.randn(out_features, 1))

        self.reset_parameters()

    #this is a hack  - need to remove later
    def reset_parameters(self):
        # Initialize weights and biases
        self.weight.data = self.weight.data.float()
        nn.init.kaiming_uniform_(self.base_layer.weight, a=math.sqrt(5))
        if self.bias:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.base_layer.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(self.base_layer.bias, -bound, bound)


    def merge(self):
        if hasattr(self, 'merged') and self.merged:
            warnings.warn("Already merged. Nothing to do.")
            return
        path_config = self.eval_config
        A = self.prepare_path(path_config['A'], self.glora_Ad, self.glora_Au).to(self.weight.dtype)
        B = self.prepare_path(path_config['B'], self.glora_Bd, self.glora_Bu).to(self.weight.dtype)
        C = self.prepare_path(path_config['C'], self.glora_Cd, self.glora_Cu).to(self.weight.dtype)
        D = self.prepare_path(path_config['D'], self.glora_D).to(self.weight.dtype)
        E = self.prepare_path(path_config['E'], self.glora_E).to(self.weight.dtype)

        print(f"forward A.shape: {A.shape}, B.shape: {B.shape}, C.shape: {C.shape}, D.shape: {D.shape}, E.shape: {E.shape}")

        self.weight.data += self.weight*A + B

        if torch.is_tensor(self.bias):
            self.bias.data += self.bias*D + E+torch.matmul(self.weight, C).squeeze()
        else:
            self.bias = nn.Parameter(E+torch.matmul(self.weight, C).squeeze())
        self.merged = True


    def forward(self, x: torch.Tensor):
        previous_dtype = x.dtype
        if self.eval_config is not None:
            path_config = self.eval_config
        else:
            path_config = random.choice(self.configs)


        A = self.prepare_path(path_config['A'], self.glora_Ad, self.glora_Au).to(self.weight.dtype)
        B = self.prepare_path(path_config['B'], self.glora_Bd, self.glora_Bu).to(self.weight.dtype)
        C = self.prepare_path(path_config['C'], self.glora_Cd, self.glora_Cu).to(self.weight.dtype)
        D = self.prepare_path(path_config['D'], self.glora_D).to(self.weight.dtype)
        E = self.prepare_path(path_config['E'], self.glora_E).to(self.weight.dtype)

        print(f"forward A.shape: {A.shape}, B.shape: {B.shape}, C.shape: {C.shape}, D.shape: {D.shape}, E.shape: {E.shape}")

        if torch.is_tensor(self.bias):
            result = F.linear(x, self.weight + self.weight*A + B, bias=self.bias + self.bias*D + E+torch.matmul(self.weight, C).squeeze())
        else:
            result = F.linear(x, self.weight + self.weight*A + B, bias=E+torch.matmul(self.weight, C).squeeze())
        result = result.to(previous_dtype)

        return result

    def prepare_path(self, config, Xd, Xu=None):
        if Xu is not None:
            if 'LoRA' in config:
                rank = int(config.split('_')[1])
                X = torch.matmul(Xd[:,:rank], Xu[:rank, :])
            elif 'vector' in config:
                X = Xd[:,0].unsqueeze(1)
            elif 'constant' in config:
                X = Xd[0,0]
            elif 'none' in config:
                X = torch.zeros(Xd.shape[0], Xu.shape[1]).to(self.weight.device)
            else:
                raise ValueError
        else:
            if 'vector' in config:
                X = Xd
            elif 'constant' in config:
                X = Xd[0]
            elif 'none' in config:
                X = torch.zeros(1).to(self.weight.device)
            else:
                raise ValueError
        return X

    def __repr__(self) -> str:
        return f"Linear({self.in_features}, {self.out_features}, bias={self.bias})"
