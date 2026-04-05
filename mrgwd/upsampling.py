"""
MR-GWD Stage 2: Temporal Upsampling Network

Takes the 256p semantic image L_t and upsamples it to the target resolution
with temporal consistency enforced via optical flow warping of the previous frame.

Architecture: Residual ConvNet (4x upsampling) + temporal fusion layer.
Optionally uses RAFT-tiny for optical flow estimation (frozen).
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Tuple


class ResBlock(nn.Module):
    def __init__(self, ch: int):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(ch, ch, 3, padding=1), nn.GELU(),
            nn.Conv2d(ch, ch, 3, padding=1),
        )

    def forward(self, x):
        return x + self.net(x)


class UpsampleBlock(nn.Module):
    def __init__(self, in_ch: int, out_ch: int):
        super().__init__()
        self.up = nn.Sequential(
            nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
            nn.Conv2d(in_ch, out_ch, 3, padding=1),
            nn.GELU(),
            ResBlock(out_ch),
        )

    def forward(self, x):
        return self.up(x)


class TemporalFusion(nn.Module):
    """Fuse current frame features with warped previous frame."""

    def __init__(self, ch: int):
        super().__init__()
        # Takes current + warped → output
        self.gate = nn.Sequential(
            nn.Conv2d(ch * 2, ch, 1),
            nn.Sigmoid(),
        )
        self.blend = nn.Conv2d(ch * 2, ch, 3, padding=1)

    def forward(self, curr: torch.Tensor, warped_prev: torch.Tensor) -> torch.Tensor:
        cat = torch.cat([curr, warped_prev], dim=1)
        gate = self.gate(cat)
        return gate * curr + (1 - gate) * self.blend(cat)


class SimpleFlowEstimator(nn.Module):
    """
    Lightweight optical flow estimator (2-frame CNN).
    Falls back to zero flow if RAFT is unavailable.
    This is a simplified version for CPU-friendly operation.
    """

    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(6, 32, 7, stride=2, padding=3), nn.GELU(),
            nn.Conv2d(32, 32, 5, stride=2, padding=2), nn.GELU(),
            nn.Conv2d(32, 16, 3, stride=2, padding=1), nn.GELU(),
            nn.Conv2d(16, 2, 3, padding=1),  # flow (u, v)
        )

    def forward(self, frame_t: torch.Tensor, frame_prev: torch.Tensor) -> torch.Tensor:
        """
        frame_t, frame_prev: (B, 3, H, W) in [-1, 1]
        returns: (B, 2, H/8, W/8) optical flow
        """
        inp = torch.cat([frame_t, frame_prev], dim=1)
        return self.net(inp)


def warp_frame(frame: torch.Tensor, flow: torch.Tensor) -> torch.Tensor:
    """
    Backward warp frame using optical flow.
    frame: (B, C, H, W)
    flow:  (B, 2, H', W')  — will be upsampled to match frame
    """
    B, C, H, W = frame.shape
    if flow.shape[-2:] != (H, W):
        flow = F.interpolate(flow, size=(H, W), mode="bilinear", align_corners=False)

    # Normalise flow to [-1, 1] grid coords
    grid_y, grid_x = torch.meshgrid(
        torch.arange(H, device=frame.device, dtype=torch.float32),
        torch.arange(W, device=frame.device, dtype=torch.float32),
        indexing="ij",
    )
    grid = torch.stack([grid_x, grid_y], dim=0).unsqueeze(0)  # (1, 2, H, W)
    grid = grid + flow
    grid[:, 0] = grid[:, 0] / (W - 1) * 2 - 1
    grid[:, 1] = grid[:, 1] / (H - 1) * 2 - 1
    grid = grid.permute(0, 2, 3, 1)  # (B, H, W, 2)
    return F.grid_sample(frame, grid, align_corners=True, padding_mode="border")


class TemporalUpsampleNet(nn.Module):
    """
    4× Residual Upsampling Network with Temporal Consistency Fusion.

    Input:  L_t (256p), prev_frame (target_resolution)
    Output: F̂_t (1024p or higher, temporally consistent)
    """

    def __init__(self, in_ch: int = 3, base_ch: int = 64, upscale: int = 4):
        super().__init__()
        assert upscale in (2, 4, 8), "upscale must be 2, 4, or 8"
        self.upscale = upscale

        # Feature extraction from L_t
        self.stem = nn.Sequential(
            nn.Conv2d(in_ch, base_ch, 7, padding=3),
            nn.GELU(),
            ResBlock(base_ch),
            ResBlock(base_ch),
        )

        # Upsampling stages
        stages = []
        ch = base_ch
        n_up = {2: 1, 4: 2, 8: 3}[upscale]
        for _ in range(n_up):
            out_ch = ch // 2 if ch > 32 else 32
            stages.append(UpsampleBlock(ch, out_ch))
            ch = out_ch
        self.upsample_stages = nn.ModuleList(stages)

        # Temporal fusion at output resolution
        self.temporal_fusion = TemporalFusion(ch)

        # Final refinement
        self.final = nn.Sequential(
            ResBlock(ch),
            ResBlock(ch),
            nn.Conv2d(ch, in_ch, 3, padding=1),
            nn.Tanh(),
        )

        # Lightweight flow estimator for temporal warp
        self.flow_estimator = SimpleFlowEstimator()

    def forward(self, L_t: torch.Tensor,
                prev_frame: Optional[torch.Tensor] = None,
                context: Optional[dict] = None) -> torch.Tensor:
        """
        L_t:       (B, 3, H_low, W_low) in [-1, 1]
        prev_frame: (B, 3, H_out, W_out) in [-1, 1] or None
        returns:   (B, 3, H_out, W_out) in [-1, 1]
        """
        squeeze = L_t.dim() == 3
        if squeeze:
            L_t = L_t.unsqueeze(0)
            if prev_frame is not None:
                prev_frame = prev_frame.unsqueeze(0)

        # Feature extraction
        x = self.stem(L_t)

        # Upsample
        for stage in self.upsample_stages:
            x = stage(x)
        # x: (B, ch, H_out, W_out)

        # Temporal fusion with warped previous frame
        if prev_frame is not None:
            # Resize prev_frame features to match current
            prev_resized = F.interpolate(prev_frame, size=x.shape[-2:],
                                         mode="bilinear", align_corners=False)
            # Estimate flow between low-res L_t and resized prev
            L_up = F.interpolate(L_t, size=x.shape[-2:], mode="bilinear", align_corners=False)
            flow = self.flow_estimator(L_up, prev_resized)
            warped = warp_frame(prev_resized, flow)

            # Fuse into 3-channel space by passing through temp conv
            x3 = self.final(x)  # preliminary output for fusion
            warped3 = warp_frame(prev_frame if prev_frame.shape[-2:] == x3.shape[-2:]
                                 else F.interpolate(prev_frame, size=x3.shape[-2:],
                                                    mode="bilinear", align_corners=False),
                                 flow)
            # weighted blend
            out = 0.85 * x3 + 0.15 * warped3
            out = out.clamp(-1.0, 1.0)
        else:
            out = self.final(x)

        return out.squeeze(0) if squeeze else out

    def temporal_consistency_loss(self, F_t: torch.Tensor,
                                  F_prev: torch.Tensor,
                                  flow: torch.Tensor) -> torch.Tensor:
        """L_temp = ||F̂_t - warp(F̂_{t-1}, flow)||_1"""
        warped = warp_frame(F_prev, flow)
        return F.l1_loss(F_t, warped)
