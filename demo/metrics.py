"""
Codec Evaluation Metrics.

Provides PSNR, SSIM, bitrate, and bits-per-pixel calculations.
"""
import torch
import torch.nn.functional as F
import math
from typing import List


def compute_psnr(original: torch.Tensor, decoded: torch.Tensor,
                 max_val: float = 2.0) -> float:
    """
    Peak Signal-to-Noise Ratio.
    Inputs in [-1, 1] → max_val = 2.0 by default.
    """
    mse = F.mse_loss(decoded.float(), original.float()).item()
    if mse < 1e-12:
        return float("inf")
    return 10 * math.log10((max_val ** 2) / mse)


def compute_ssim(original: torch.Tensor, decoded: torch.Tensor,
                 window_size: int = 11) -> float:
    """
    Structural Similarity Index (simplified, single-scale).
    Tensors: (3, H, W) or (1, 3, H, W) in [-1, 1].
    """
    if original.dim() == 3:
        original = original.unsqueeze(0)
        decoded = decoded.unsqueeze(0)

    # Gaussian window
    sigma = 1.5
    coords = torch.arange(window_size, dtype=torch.float32) - window_size // 2
    g = torch.exp(-(coords ** 2) / (2 * sigma ** 2))
    g = g / g.sum()
    window = g.outer(g).unsqueeze(0).unsqueeze(0)  # (1,1,ws,ws)
    window = window.repeat(3, 1, 1, 1)             # (3,1,ws,ws)

    C1, C2 = 0.01 ** 2, 0.03 ** 2  # stability constants

    mu1 = F.conv2d(original, window, padding=window_size//2, groups=3)
    mu2 = F.conv2d(decoded, window, padding=window_size//2, groups=3)
    mu1_sq = mu1 ** 2
    mu2_sq = mu2 ** 2
    mu1_mu2 = mu1 * mu2
    sigma1_sq = F.conv2d(original ** 2, window, padding=window_size//2, groups=3) - mu1_sq
    sigma2_sq = F.conv2d(decoded ** 2, window, padding=window_size//2, groups=3) - mu2_sq
    sigma12 = F.conv2d(original * decoded, window, padding=window_size//2, groups=3) - mu1_mu2

    ssim_map = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / \
               ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))
    return ssim_map.mean().item()


def compute_bitrate(packet_bytes_list: List[int], fps: float = 30.0) -> float:
    """
    Compute average bitrate in Mbps given per-frame packet sizes (bytes) and fps.
    """
    if not packet_bytes_list:
        return 0.0
    total_bytes = sum(packet_bytes_list)
    n_frames = len(packet_bytes_list)
    duration_s = n_frames / fps
    return (total_bytes * 8) / duration_s / 1e6  # Mbps


def compute_bpp(packet_bytes: int, width: int, height: int) -> float:
    """Bits per pixel for a single frame's packet."""
    n_pixels = width * height
    return (packet_bytes * 8) / n_pixels


class MetricsAccumulator:
    """Rolling metrics accumulator for streaming evaluation."""

    def __init__(self, fps: float = 30.0):
        self.fps = fps
        self.psnr_values: List[float] = []
        self.ssim_values: List[float] = []
        self.packet_bytes: List[int] = []
        self.keyframe_indices: List[int] = []
        self.lcc_trigger_indices: List[int] = []

    def update(self, original, decoded, packet_bytes: int,
               is_keyframe: bool = False, lcc_triggered: bool = False):
        frame_idx = len(self.packet_bytes)
        with torch.no_grad():
            self.psnr_values.append(compute_psnr(original, decoded))
            self.ssim_values.append(compute_ssim(original, decoded))
        self.packet_bytes.append(packet_bytes)
        if is_keyframe:
            self.keyframe_indices.append(frame_idx)
        if lcc_triggered:
            self.lcc_trigger_indices.append(frame_idx)

    @property
    def mean_psnr(self) -> float:
        return sum(self.psnr_values) / max(1, len(self.psnr_values))

    @property
    def mean_ssim(self) -> float:
        return sum(self.ssim_values) / max(1, len(self.ssim_values))

    @property
    def avg_bitrate_mbps(self) -> float:
        return compute_bitrate(self.packet_bytes, self.fps)

    @property
    def instantaneous_bitrate_mbps(self) -> float:
        """Bitrate over last 30 frames."""
        recent = self.packet_bytes[-30:]
        return compute_bitrate(recent, self.fps)

    def summary(self) -> dict:
        return {
            "frames": len(self.packet_bytes),
            "avg_psnr_db": round(self.mean_psnr, 2),
            "avg_ssim": round(self.mean_ssim, 4),
            "avg_bitrate_mbps": round(self.avg_bitrate_mbps, 4),
            "keyframes": len(self.keyframe_indices),
            "lcc_triggers": len(self.lcc_trigger_indices),
            "total_bytes": sum(self.packet_bytes),
        }
