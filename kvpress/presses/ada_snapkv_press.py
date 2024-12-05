# Author: Yuan Feng
# Corresponding Paper: Ada-KV


import inspect
import math
from dataclasses import dataclass

import torch
import torch.nn.functional as F
from torch import nn
from transformers.models.llama.modeling_llama import repeat_kv, rotate_half
import transformers.modeling_utils as modeling_utils
from kvpress.presses.base_press import AdaBasePress


@dataclass
class AdaSnapKVPress(AdaBasePress):
    """
    SnapKV (https://arxiv.org/abs/2404.14469) use the attention of the latest window_size tokens to estimate the
    importance of the previous KV pairs. We use the default settings from:
    https://github.com/FasterDecoding/SnapKV/blob/main/snapkv/monkeypatch/snapkv_utils.py#L24
    """

    compression_ratio: float = 0.0
    window_size: int = 64
    kernel_size: int = 5
    floor_alpha: float = 0.2

    def compute_window_attention(self, module, hidden_states, keys):
        """
        Compute the last window_size queries and associated attention weights for the first q_len - window_size keys.
        """

        bsz, q_len, _ = hidden_states.shape

        # Get last window_size queries
        if hasattr(module, "q_proj"):
            query_states = module.q_proj(hidden_states[:, -self.window_size :])
        elif hasattr(module, "qkv_proj"):
            qkv = module.qkv_proj(hidden_states[:, -self.window_size :])
            query_states = qkv[..., : module.num_heads * module.head_dim]
        else:
            raise NotImplementedError(f"SnapKV not yet implemented for {module.__class__}.")

        query_states = query_states.view(bsz, self.window_size, module.num_heads, module.head_dim).transpose(1, 2)

        # Apply RoPE
        if "position_ids" in inspect.signature(module.rotary_emb.forward).parameters:
            position_ids = torch.arange(q_len - self.window_size, q_len).unsqueeze(0).to(query_states.device)
            cos, sin = module.rotary_emb(query_states, position_ids)
        else:
            cos, sin = module.rotary_emb(query_states, q_len)
            cos, sin = cos[-self.window_size :].unsqueeze(0), sin[-self.window_size :].unsqueeze(0)
        query_states = (query_states * cos) + (rotate_half(query_states) * sin)

        # Compute attention for first q_len - window_size tokens
        key_states = repeat_kv(keys, module.num_key_value_groups)
        attn_weights = torch.matmul(query_states, key_states.transpose(2, 3)) / math.sqrt(module.head_dim)
        attention_mask = torch.ones_like(attn_weights) * float("-inf")
        attention_mask = torch.triu(attention_mask, diagonal=q_len - self.window_size + 1)
        attn_weights += attention_mask
        attn_weights = nn.functional.softmax(attn_weights, dim=-1, dtype=torch.float32).to(query_states.dtype)
        attn_weights = attn_weights[..., : -self.window_size]

        return attn_weights


    """
        using mask to identify the KV Selection, with the selected KV pairs with MAX mask value
    """
    def score(
        self,
        module: nn.Module,
        hidden_states: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attentions: torch.Tensor,
        kwargs,
    ) -> torch.Tensor:
        
        cache_metadata = kwargs.get("metadata", None)
        assert cache_metadata is not None, "cache_metadata is required for AdaSnapKVPress"
        
        # Current implementation only allows to compress once
        # check if first time compression
        head_lens = cache_metadata.head_lens
        assert all(x == head_lens[0] for x in head_lens), "Not all elements in head_lens are the same, implying multiple compressions"


        # convert to (bsz, num_key_value_heads, q_len, head_dim) for easy score
        keys = keys.view(cache_metadata.bsz, cache_metadata.num_key_value_heads, cache_metadata.head_lens[0], keys.shape[-1])
        values = values.view(cache_metadata.bsz, cache_metadata.num_key_value_heads, cache_metadata.head_lens[0], keys.shape[-1])


        bsz, num_key_value_heads, q_len, _ = keys.shape

        assert q_len > self.window_size, "Query length should be greater than the window size"

        if attentions is not None:
            attn_weights = attentions[..., -self.window_size :, : -self.window_size]
        else:
            attn_weights = self.compute_window_attention(module, hidden_states, keys)

        scores = attn_weights.mean(dim=-2)
        scores = F.avg_pool1d(scores, kernel_size=self.kernel_size, padding=self.kernel_size // 2, stride=1)

        # Average per grioup (https://github.com/FasterDecoding/SnapKV/issues/22)
        scores = scores.view(bsz, num_key_value_heads, module.num_key_value_groups, q_len - self.window_size)
        scores = scores.mean(2)

        # safe guard for each head AdaKV
        compress_q_len = q_len * (1 - self.compression_ratio) * self.floor_alpha
        topk_idx = scores.topk(int(compress_q_len), dim=-1).indices
        scores.scatter_(-1, topk_idx, torch.finfo(scores.dtype).max)

        # Add back the observation window. Use max score to make sure the window is not pruned.
        scores = F.pad(scores, (0, self.window_size), value=scores.max().item())

        return scores