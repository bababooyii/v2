"""
Adaptive Lloyd-Max Scalar Quantizer for GTM.

Fits optimal codebooks for any bit-width via iterative Lloyd-Max algorithm,
then provides fast quantize/dequantize operations for latent residuals.
"""
import torch
import numpy as np
from typing import Tuple, Optional
import pickle


class LloydMaxQuantizer:
    """
    1-D Lloyd-Max quantizer with learnable codebooks.

    Supports 1-8 bit quantization. Codebooks are fit on calibration data
    and can be saved/loaded for deterministic deployment.
    """

    def __init__(self, n_bits: int, n_iter: int = 100):
        assert 1 <= n_bits <= 8, "bit-width must be 1..8"
        self.n_bits = n_bits
        self.n_levels = 2 ** n_bits
        self.n_iter = n_iter
        self.centroids: Optional[torch.Tensor] = None   # (n_levels,)
        self.boundaries: Optional[torch.Tensor] = None  # (n_levels+1,)

    # ------------------------------------------------------------------
    def fit(self, data: torch.Tensor) -> "LloydMaxQuantizer":
        """
        Calibrate codebook on 1-D data tensor.
        data: arbitrary shape, will be flattened.
        """
        x = data.float().flatten().numpy()
        # Init centroids by uniform percentile spacing
        percentiles = np.linspace(0, 100, self.n_levels)
        centroids = np.percentile(x, percentiles)

        for _ in range(self.n_iter):
            # Boundaries = midpoints between centroids
            boundaries = np.concatenate([
                [-np.inf],
                (centroids[:-1] + centroids[1:]) / 2.0,
                [np.inf],
            ])
            # Update centroids = conditional mean in each cell
            new_centroids = np.zeros_like(centroids)
            for k in range(self.n_levels):
                mask = (x >= boundaries[k]) & (x < boundaries[k + 1])
                if mask.any():
                    new_centroids[k] = x[mask].mean()
                else:
                    new_centroids[k] = centroids[k]
            if np.max(np.abs(new_centroids - centroids)) < 1e-8:
                break
            centroids = new_centroids

        self.centroids = torch.tensor(centroids, dtype=torch.float32)
        boundaries_t = np.concatenate([[-np.inf], (centroids[:-1] + centroids[1:]) / 2.0, [np.inf]])
        self.boundaries = torch.tensor(boundaries_t, dtype=torch.float32)
        return self

    def default_fit(self, scale: float = 1.0) -> "LloydMaxQuantizer":
        """Quick init: Gaussian-optimal boundaries without calibration data."""
        import scipy.stats as stats
        q_pts = np.linspace(0.5 / self.n_levels, 1 - 0.5 / self.n_levels, self.n_levels)
        centroids = stats.norm.ppf(q_pts) * scale
        self.centroids = torch.tensor(centroids, dtype=torch.float32)
        boundaries = np.concatenate([
            [-np.inf],
            (centroids[:-1] + centroids[1:]) / 2,
            [np.inf],
        ])
        self.boundaries = torch.tensor(boundaries, dtype=torch.float32)
        return self

    # ------------------------------------------------------------------
    def quantize(self, x: torch.Tensor) -> torch.Tensor:
        """
        Quantize scalar values to integer indices.
        x: arbitrary shape → same shape of int indices.
        """
        assert self.centroids is not None, "Call fit() or default_fit() first"
        centroids = self.centroids.to(x.device)
        # Nearest-centroid assignment via broadcasting
        diff = (x.unsqueeze(-1) - centroids).abs()  # (..., n_levels)
        return diff.argmin(dim=-1).to(torch.int16)

    def dequantize(self, indices: torch.Tensor) -> torch.Tensor:
        """Map integer indices back to centroid values."""
        assert self.centroids is not None
        centroids = self.centroids.to(indices.device)
        return centroids[indices.long()]

    # ------------------------------------------------------------------
    def save(self, path: str):
        with open(path, "wb") as f:
            pickle.dump({"centroids": self.centroids, "boundaries": self.boundaries,
                         "n_bits": self.n_bits}, f)

    @classmethod
    def load(cls, path: str) -> "LloydMaxQuantizer":
        with open(path, "rb") as f:
            d = pickle.load(f)
        obj = cls(n_bits=d["n_bits"])
        obj.centroids = d["centroids"]
        obj.boundaries = d["boundaries"]
        return obj
