"""
Recursive Polar (Hyperspherical) Decomposition for GTM.

Converts a D-dim Cartesian vector into (radius, D-1 angles) representation,
enabling independent scalar quantization of each hyperspherical coordinate.
"""
import torch
import math
from typing import Tuple


def polar_transform(v: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Convert Cartesian vector(s) to hyperspherical coordinates.

    Args:
        v: (..., D)
    Returns:
        r:      (...,)     - L2 norm (radius)
        thetas: (..., D-1) - angles in [0, pi] except last in [0, 2*pi)
    """
    eps = 1e-8
    D = v.shape[-1]
    r = torch.norm(v, dim=-1).clamp(min=eps)          # (...,)
    v_norm = v / r.unsqueeze(-1)                       # (..., D)  unit vector

    thetas = []
    # recursive: theta_i = arccos(x_i / sqrt(x_i^2 + ... + x_D^2))
    running_sq = (v_norm ** 2).flip(-1).cumsum(-1).flip(-1)  # (..., D)

    for i in range(D - 1):
        numer = v_norm[..., i]
        denom = running_sq[..., i].clamp(min=eps).sqrt()
        cos_theta = (numer / denom).clamp(-1.0, 1.0)
        theta = torch.acos(cos_theta)                  # (...,) in [0, pi]
        # Last angle: extend to [0, 2*pi) using sign of last component
        if i == D - 2:
            sign = torch.sign(v_norm[..., -1])
            sign[sign == 0] = 1.0
            theta = torch.where(sign < 0, 2 * math.pi - theta, theta)
        thetas.append(theta)

    thetas = torch.stack(thetas, dim=-1)               # (..., D-1)
    return r, thetas


def polar_inverse(r: torch.Tensor, thetas: torch.Tensor) -> torch.Tensor:
    """
    Convert hyperspherical coordinates back to Cartesian.

    Args:
        r:      (...,)
        thetas: (..., D-1)
    Returns:
        v: (..., D)
    """
    D = thetas.shape[-1] + 1
    sin_prods = torch.ones(*thetas.shape[:-1], device=thetas.device, dtype=thetas.dtype)  # (...,)
    coords = []
    for i in range(D - 1):
        theta_i = thetas[..., i]
        coords.append(sin_prods * torch.cos(theta_i))
        sin_prods = sin_prods * torch.sin(theta_i)
    coords.append(sin_prods)                           # last: product of all sines
    v_unit = torch.stack(coords, dim=-1)               # (..., D)
    return v_unit * r.unsqueeze(-1)
