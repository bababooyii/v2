"""
Sparse Latent Coding — transmits only top-k components of Δz_t.

Instead of transmitting the full residual vector, OmniQuant-Apex
identifies the k most significant components by magnitude and transmits
only their indices + values. This dramatically reduces bit overhead
for small residuals.
"""
import torch
from typing import Tuple


class SparseCoder:
    """
    Top-k sparse encoder/decoder for latent residuals.

    Theory: If Δz_t is near-zero (good prediction), most components
    are negligible. Transmitting only top-k captures ~95% of the
    energy in the residual with ~k/D of the bits.
    """

    def __init__(self, top_k_fraction: float = 0.25, min_k: int = 8):
        self.top_k_fraction = top_k_fraction
        self.min_k = min_k

    def get_k(self, dim: int) -> int:
        return max(self.min_k, int(dim * self.top_k_fraction))

    def encode_sparse(self, delta_z: torch.Tensor,
                      k: int = None) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Select top-k components of delta_z by absolute value.

        delta_z: (D,)
        returns: (indices: LongTensor[k], values: FloatTensor[k])
        """
        D = delta_z.shape[0]
        k = k or self.get_k(D)
        k = min(k, D)

        magnitudes = delta_z.abs()
        top_k = torch.topk(magnitudes, k=k, largest=True, sorted=True)
        indices = top_k.indices                          # (k,) int64
        values = delta_z[indices]                        # (k,) float
        return indices, values

    def decode_sparse(self, indices: torch.Tensor,
                      values: torch.Tensor,
                      dim: int) -> torch.Tensor:
        """
        Reconstruct dense delta_z from sparse (indices, values) pair.

        indices: (k,) int64
        values:  (k,) float
        dim:     full dimension D
        returns: (D,) dense Δz reconstruction
        """
        delta_z = torch.zeros(dim, dtype=values.dtype, device=values.device)
        delta_z.scatter_(0, indices, values)
        return delta_z

    def energy_retention(self, delta_z: torch.Tensor, k: int = None) -> float:
        """
        Compute fraction of energy (L2 squared) retained by top-k sparse code.
        Useful for bitrate-quality analysis.
        """
        D = delta_z.shape[0]
        k = k or self.get_k(D)
        total_energy = (delta_z ** 2).sum().item()
        if total_energy < 1e-12:
            return 1.0
        sorted_sq, _ = (delta_z ** 2).sort(descending=True)
        topk_energy = sorted_sq[:k].sum().item()
        return topk_energy / total_energy

    def estimate_bits(self, dim: int, k: int = None, index_bits: int = None,
                      value_bits: int = 4) -> int:
        """
        Estimate total bits required to transmit sparse representation.

        - Each index needs ceil(log2(D)) bits
        - Each value needs value_bits (quantized)
        """
        import math
        k = k or self.get_k(dim)
        index_bits = index_bits or math.ceil(math.log2(max(dim, 2)))
        return k * (index_bits + value_bits)
