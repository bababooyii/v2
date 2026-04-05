"""
QJL (Quantized Johnson-Lindenstrauss) Bias Correction for GTM.

After Lloyd-Max quantization, a systematic bias remains in the residual.
QJL projects the residual into a lower-dimensional sketch via a random
Johnson-Lindenstrauss matrix, 1-bit-quantizes the signs of the projection,
and transmits these sign bits as side information for the decoder to apply
a bias correction nudge.

This cuts mean quantization error significantly at the cost of only
proj_dim / 8 extra bytes per vector.
"""
import torch
import math


class QJL:
    """
    1-bit Johnson-Lindenstrauss sketch-based bias correction.

    Usage:
        qjl = QJL(proj_dim=64, seed=137)
        bits = qjl.encode(residual)          # residual = original - quantized
        correction = qjl.decode(bits, dim)   # apply at decoder
    """

    def __init__(self, proj_dim: int = 64, seed: int = 137):
        self.proj_dim = proj_dim
        self.seed = seed
        self._proj: torch.Tensor = None  # lazy

    def _get_projection(self, dim: int, device: torch.device) -> torch.Tensor:
        """Generate or retrieve the random JL matrix (proj_dim x dim)."""
        if self._proj is None or self._proj.shape[1] != dim:
            rng = torch.Generator(device="cpu")
            rng.manual_seed(self.seed)
            # Rademacher ±1/sqrt(proj_dim) entries
            mat = (torch.randint(0, 2, (self.proj_dim, dim), generator=rng) * 2 - 1).float()
            mat = mat / math.sqrt(self.proj_dim)
            self._proj = mat
        return self._proj.to(device)

    def encode(self, residual: torch.Tensor) -> torch.Tensor:
        """
        Compute 1-bit sketch of the residual vector.

        residual: (D,)  or  (B, D)
        returns:  (proj_dim,) or (B, proj_dim)  bool tensor of sign bits
        """
        P = self._get_projection(residual.shape[-1], residual.device)
        projected = residual.float() @ P.T   # (..., proj_dim)
        return projected > 0                 # bool sign bits

    def decode(self, sign_bits: torch.Tensor, orig_dim: int) -> torch.Tensor:
        """
        Reconstruct approximate residual from 1-bit signs.

        sign_bits: (..., proj_dim) bool
        returns:   (..., orig_dim) correction vector
        """
        P = self._get_projection(orig_dim, sign_bits.device)
        # Map bool → ±1, then pseudo-invert sketch via P^T
        s = sign_bits.float() * 2 - 1       # (..., proj_dim)
        correction = s @ P / self.proj_dim  # (..., orig_dim)
        return correction

    def state_dict(self):
        return {"proj_dim": self.proj_dim, "seed": self.seed}

    @classmethod
    def from_state_dict(cls, d):
        return cls(proj_dim=d["proj_dim"], seed=d["seed"])
