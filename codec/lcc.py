"""
Proactive Latent Consistency Check (LCC).

Detects when quantization + prediction errors have become significant
enough to trigger a keyframe refresh, preventing semantic drift.

The LCC computes a metric between the original residual Δz_t and
a preview-quantized version Δz̃_t. If the difference exceeds a threshold,
it forces a keyframe.
"""
import torch
import torch.nn.functional as F
from typing import Literal


class LCC:
    """
    Latent Consistency Check.

    Monitors quantization error in the latent residual and triggers
    keyframe promotion when semantic drift becomes significant.

    Methods:
        'l2'     : ||Δz_t - Δz̃_t||_2                    (default)
        'cosine' : 1 - cosine_similarity(Δz_t, Δz̃_t)
        'norm'   : ||Δz_t||_2 / ||z_{t-1}||_2           (relative magnitude)
    """

    def __init__(self, threshold: float = 0.15,
                 method: Literal["l2", "cosine", "norm"] = "l2",
                 history_len: int = 5):
        self.threshold = threshold
        self.method = method
        self.history_len = history_len
        self._error_history = []
        self.trigger_count = 0

    def check(self, delta_z: torch.Tensor,
              delta_z_tilde: torch.Tensor,
              z_ref: torch.Tensor = None) -> bool:
        """
        Check whether quantization error warrants a keyframe.

        delta_z:       (D,) original residual
        delta_z_tilde: (D,) preview-quantized residual
        z_ref:         (D,) reference latent for norm-based method

        Returns True if keyframe should be forced.
        """
        err = self._compute_error(delta_z, delta_z_tilde, z_ref)
        self._error_history.append(err)
        if len(self._error_history) > self.history_len:
            self._error_history.pop(0)

        is_triggered = err > self.threshold
        if is_triggered:
            self.trigger_count += 1
        return is_triggered

    def check_prediction_divergence(self, z_t: torch.Tensor,
                                    z_hat_t: torch.Tensor) -> bool:
        """
        Additional check: if prediction ẑ_t is very far from z_t,
        force keyframe regardless of quantization quality.
        """
        cos_sim = F.cosine_similarity(z_t.unsqueeze(0), z_hat_t.unsqueeze(0)).item()
        diverged = cos_sim < (1.0 - self.threshold * 2)
        if diverged:
            self.trigger_count += 1
        return diverged

    def _compute_error(self, delta_z: torch.Tensor,
                       delta_z_tilde: torch.Tensor,
                       z_ref: torch.Tensor = None) -> float:
        if self.method == "l2":
            return (delta_z - delta_z_tilde).norm().item()
        elif self.method == "cosine":
            norm_a = delta_z.norm()
            norm_b = delta_z_tilde.norm()
            if norm_a < 1e-8 or norm_b < 1e-8:
                return 0.0
            return 1.0 - F.cosine_similarity(
                delta_z.unsqueeze(0), delta_z_tilde.unsqueeze(0)
            ).item()
        elif self.method == "norm":
            if z_ref is None:
                return (delta_z - delta_z_tilde).norm().item()
            ref_norm = z_ref.norm().clamp(min=1e-8)
            return (delta_z - delta_z_tilde).norm().item() / ref_norm.item()
        else:
            raise ValueError(f"Unknown LCC method: {self.method}")

    @property
    def running_mean_error(self) -> float:
        if not self._error_history:
            return 0.0
        return sum(self._error_history) / len(self._error_history)

    def reset(self):
        self._error_history.clear()
        self.trigger_count = 0

    def set_threshold(self, threshold: float):
        self.threshold = threshold
