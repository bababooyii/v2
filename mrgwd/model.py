"""
MR-GWD — Multi-Resolution Generative World Decoder

Composes LatentDiffusionSynth + TemporalUpsampleNet into a unified model.
Manages the previous-frame buffer for temporal consistency.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional
from pathlib import Path

from .latent_diffusion import LatentDiffusionSynth
from .upsampling import TemporalUpsampleNet


class MRGWD(nn.Module):
    """
    Multi-Resolution Generative World Decoder.

    Usage:
        mrgwd = MRGWD(latent_dim=512, output_size=(1080, 1920))
        mrgwd.reset_state()
        for z_t in latent_stream:
            frame = mrgwd.synthesize(z_t)   # returns (3, H, W) tensor in [-1,1]
    """

    def __init__(self, latent_dim: int = 512,
                 output_size: tuple = (512, 512),
                 use_vae: bool = False,
                 upscale: int = 4):
        super().__init__()
        self.latent_dim = latent_dim
        self.output_size = output_size

        # Stage 1: z_t → 256p image
        self.latent_synth = LatentDiffusionSynth(latent_dim=latent_dim, use_vae=use_vae)

        # Stage 2: 256p → output_size image
        self.upsample_net = TemporalUpsampleNet(upscale=upscale)

        # State
        self._prev_frame: Optional[torch.Tensor] = None

    # ------------------------------------------------------------------

    def synthesize(self, z_t: torch.Tensor,
                   prev_frame: Optional[torch.Tensor] = None) -> torch.Tensor:
        """
        Synthesize a frame from z_t.

        z_t:       (D,) latent state
        prev_frame: (3, H, W) optional override; uses internal state if None
        returns:   (3, H_out, W_out) in [-1, 1]
        """
        prev = prev_frame if prev_frame is not None else self._prev_frame

        with torch.no_grad():
            # Stage 1: semantic image
            L_t = self.latent_synth(z_t)                          # (3, 256, 256)

            # Resize L_t to target if needed (keep aspect ratio)
            if L_t.shape[-2:] != (256, 256):
                L_t = F.interpolate(L_t.unsqueeze(0), size=(256, 256),
                                    mode="bilinear", align_corners=False).squeeze(0)

            # Stage 2: upsample with temporal consistency
            F_hat = self.upsample_net(L_t, prev)                  # (3, H_out, W_out)

            # Resize to desired output
            if F_hat.shape[-2:] != self.output_size:
                F_hat = F.interpolate(F_hat.unsqueeze(0),
                                      size=self.output_size,
                                      mode="bilinear", align_corners=False).squeeze(0)

        self._prev_frame = F_hat.detach()
        return F_hat

    def synthesize_batch(self, z_batch: torch.Tensor) -> torch.Tensor:
        """Synthesize multiple frames sequentially (temporal consistency maintained)."""
        frames = []
        for z in z_batch:
            frames.append(self.synthesize(z))
        return torch.stack(frames)

    def reset_state(self):
        self._prev_frame = None

    # ------------------------------------------------------------------

    def save(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "latent_synth_projector": self.latent_synth.projector.state_dict(),
            "latent_synth_decoder": self.latent_synth.decoder.state_dict() if not self.latent_synth.use_vae else None,
            "upsample_net": self.upsample_net.state_dict(),
            "latent_dim": self.latent_dim,
            "output_size": self.output_size,
        }, p)

    def load(self, path: str):
        ckpt = torch.load(path, map_location="cpu")
        self.latent_synth.projector.load_state_dict(ckpt["latent_synth_projector"])
        if ckpt.get("latent_synth_decoder") and not self.latent_synth.use_vae:
            self.latent_synth.decoder.load_state_dict(ckpt["latent_synth_decoder"])
        self.upsample_net.load_state_dict(ckpt["upsample_net"])
        return self
