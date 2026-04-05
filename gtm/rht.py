"""
Randomized Hadamard Transform (RHT) for the GTM.

Applies a Rademacher sign flip then a fast Walsh-Hadamard Transform to
spread energy uniformly before polar quantization.
"""
import torch
import math


def _next_power_of_two(n: int) -> int:
    return 1 << (n - 1).bit_length()


def _fwht(x: torch.Tensor) -> torch.Tensor:
    """Fast Walsh-Hadamard Transform (in-place, unnormalized)."""
    n = x.shape[-1]
    assert (n & (n - 1)) == 0, "Length must be a power of 2"
    h = 1
    while h < n:
        x = x.clone()
        for i in range(0, n, h * 2):
            x[..., i:i+h], x[..., i+h:i+2*h] = (
                x[..., i:i+h] + x[..., i+h:i+2*h],
                x[..., i:i+h] - x[..., i+h:i+2*h],
            )
        h *= 2
    return x


class RHT:
    """
    Randomized Hadamard Transform.

    v' = D @ H @ v  where D is a random Rademacher diagonal.
    Normalised so that the transform is an isometry (up to sign).
    """

    def __init__(self, dim: int, seed: int = 42):
        self.dim = dim
        self.padded_dim = _next_power_of_two(dim)
        rng = torch.Generator()
        rng.manual_seed(seed)
        signs = (torch.randint(0, 2, (self.padded_dim,), generator=rng) * 2 - 1).float()
        self.register_buffer_signs = signs  # keep for serialisation
        self._signs = signs

    def forward(self, v: torch.Tensor) -> torch.Tensor:
        """Apply RHT. v: (..., dim) → (..., padded_dim)."""
        # Pad if needed
        if v.shape[-1] < self.padded_dim:
            pad = torch.zeros(*v.shape[:-1], self.padded_dim - v.shape[-1], device=v.device, dtype=v.dtype)
            v = torch.cat([v, pad], dim=-1)
        signs = self._signs.to(v.device)
        vd = v * signs
        vh = _fwht(vd)
        return vh / math.sqrt(self.padded_dim)

    def inverse(self, v_hat: torch.Tensor) -> torch.Tensor:
        """Invert RHT. v_hat: (..., padded_dim) → (..., dim)."""
        signs = self._signs.to(v_hat.device)
        # Hadamard is its own inverse (up to scale), so H^{-1} = H / n
        scaled = v_hat * math.sqrt(self.padded_dim)
        hv = _fwht(scaled) / self.padded_dim
        return (hv * signs)[..., :self.dim]

    def to(self, device):
        self._signs = self._signs.to(device)
        return self

    def state_dict(self):
        return {"signs": self._signs, "dim": self.dim}

    @classmethod
    def from_state_dict(cls, sd):
        obj = cls.__new__(cls)
        obj.dim = sd["dim"]
        obj._signs = sd["signs"]
        obj.padded_dim = _next_power_of_two(obj.dim)
        obj.register_buffer_signs = obj._signs
        return obj
