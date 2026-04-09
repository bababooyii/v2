"""
MR-GWD Stage 1: Latent Diffusion Synthesis

Projects z_t (semantic latent) into the VAE latent space and decodes
it to a lower-resolution 256p semantic image L_t using a pretrained
Stable Diffusion VAE decoder (frozen).

Falls back to a lightweight ConvTranspose network when running CPU-only.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional


class LatentProjector(nn.Module):
    """
    Learned linear projection from ULEP latent space (D) to
    SD-VAE latent space (4 x H_vae x W_vae).
    We target 256p output → VAE latent is 4x32x32.
    """

    VAE_LATENT_SHAPE = (4, 32, 32)   # for 256x256 output

    def __init__(self, latent_dim: int = 512):
        super().__init__()
        vae_dim = 4 * 32 * 32  # 4096
        self.proj = nn.Sequential(
            nn.Linear(latent_dim, vae_dim * 2),
            nn.GELU(),
            nn.Linear(vae_dim * 2, vae_dim),
        )

    def forward(self, z_t: torch.Tensor) -> torch.Tensor:
        """z_t: (B, D) → (B, 4, 32, 32)"""
        vae_flat = self.proj(z_t)
        return vae_flat.view(-1, *self.VAE_LATENT_SHAPE)


class ConvFallbackDecoder(nn.Module):
    """
    CPU-friendly fallback: simple ConvTranspose decoder.
    Inputs z_t (D,) and outputs (3, 256, 256).
    """

    def __init__(self, latent_dim: int = 512):
        super().__init__()
        self.fc = nn.Linear(latent_dim, 512 * 4 * 4)
        self.decoder = nn.Sequential(
            nn.ConvTranspose2d(512, 256, 4, stride=2, padding=1),   # 8x8
            nn.GELU(),
            nn.ConvTranspose2d(256, 128, 4, stride=2, padding=1),   # 16x16
            nn.GELU(),
            nn.ConvTranspose2d(128, 64, 4, stride=2, padding=1),    # 32x32
            nn.GELU(),
            nn.ConvTranspose2d(64, 32, 4, stride=2, padding=1),     # 64x64
            nn.GELU(),
            nn.ConvTranspose2d(32, 16, 4, stride=2, padding=1),     # 128x128
            nn.GELU(),
            nn.ConvTranspose2d(16, 3, 4, stride=2, padding=1),      # 256x256
            nn.Tanh(),
        )

    def forward(self, z_t: torch.Tensor) -> torch.Tensor:
        """z_t: (B, D) → (B, 3, 256, 256) in [-1, 1]"""
        h = self.fc(z_t).view(-1, 512, 4, 4)
        return self.decoder(h)


class LatentDiffusionSynth(nn.Module):
    """
    Stage 1 synthesis: z_t → L_t (256p semantic image).

    Uses pretrained SD-VAE decoder for high quality output.
    Falls back to ConvFallbackDecoder for CPU-only environments.
    """

    def __init__(self, latent_dim: int = 512, use_vae: bool = True, force_vae: bool = False):
        super().__init__()
        self.latent_dim = latent_dim
        # Use VAE if: requested AND (GPU available OR force=True)
        self.use_vae = use_vae and (torch.cuda.is_available() or force_vae)

        self.projector = LatentProjector(latent_dim)

        if self.use_vae:
            self._load_vae()
        else:
            print("[MR-GWD] Using ConvFallbackDecoder (CPU mode)")
            self.decoder = ConvFallbackDecoder(latent_dim)

        self._scale_factor = 0.18215  # SD VAE scaling constant

    def _load_vae(self):
        try:
            from diffusers import AutoencoderKL
            self.vae = AutoencoderKL.from_pretrained(
                "stabilityai/sd-vae-ft-mse", torch_dtype=torch.float16
            )
            for p in self.vae.parameters():
                p.requires_grad_(False)
            print("[MR-GWD] Loaded SD-VAE decoder (fp16)")
        except Exception as e:
            print(f"[MR-GWD] VAE load failed ({e}), using ConvFallback.")
            self.use_vae = False
            self.decoder = ConvFallbackDecoder(self.latent_dim)

    def forward(self, z_t: torch.Tensor) -> torch.Tensor:
        """
        z_t: (B, D) or (D,) → L_t: (B, 3, 256, 256) in [-1, 1]
        """
        squeeze = z_t.dim() == 1
        if squeeze:
            z_t = z_t.unsqueeze(0)

        if self.use_vae:
            vae_latent = self.projector(z_t.float())              # (B, 4, 32, 32)
            vae_latent = vae_latent.to(next(self.vae.parameters()).device)
            vae_latent = vae_latent.half() if next(self.vae.parameters()).dtype == torch.float16 else vae_latent
            vae_latent = vae_latent / self._scale_factor
            L_t = self.vae.decode(vae_latent).sample             # (B, 3, 256, 256)
            L_t = L_t.float()
        else:
            L_t = self.decoder(z_t.float())                      # (B, 3, 256, 256)

        L_t = L_t.clamp(-1.0, 1.0)
        return L_t.squeeze(0) if squeeze else L_t
