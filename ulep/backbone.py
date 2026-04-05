"""
ULEP Backbone — wraps DINOv2-small (frozen) for multi-scale feature extraction.
Falls back to a lightweight ConvNet if transformers are unavailable.
"""
import torch
import torch.nn as nn
from typing import List


class ULEPBackbone(nn.Module):
    """
    Frozen DINOv2-small backbone.
    Outputs patch-level features from multiple ViT layers for
    rich multi-scale semantic representation.
    """

    DINO_MODEL = "facebook/dinov2-small"   # 384-dim, 197 patches at 224x224

    def __init__(self, use_pretrained: bool = True, extract_layers: List[int] = None):
        super().__init__()
        self.extract_layers = extract_layers or [3, 6, 9, 11]  # 4 depths
        self.feat_dim = 384  # DINOv2-small hidden dim

        if use_pretrained:
            self._load_dino()
        else:
            self._build_fallback()

    def _load_dino(self):
        try:
            from transformers import AutoModel
            self.model = AutoModel.from_pretrained(self.DINO_MODEL)
            for p in self.model.parameters():
                p.requires_grad_(False)
            self.mode = "dino"
        except Exception as e:
            print(f"[ULEP] DINOv2 unavailable ({e}), using fallback ConvNet backbone.")
            self._build_fallback()

    def _build_fallback(self):
        """Lightweight ConvNet backbone (CPU-friendly)."""
        self.model = nn.Sequential(
            nn.Conv2d(3, 64, 7, stride=2, padding=3), nn.GELU(),
            nn.Conv2d(64, 128, 3, stride=2, padding=1), nn.GELU(),
            nn.Conv2d(128, 256, 3, stride=2, padding=1), nn.GELU(),
            nn.Conv2d(256, 384, 3, stride=2, padding=1), nn.GELU(),
            nn.AdaptiveAvgPool2d((14, 14)),
        )
        self.feat_dim = 384
        self.mode = "conv"

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: (B, 3, H, W) — normalised to [0,1] or ImageNet stats
        returns: (B, N_patches, feat_dim)
        """
        if self.mode == "dino":
            out = self.model(x, output_hidden_states=True)
            # Aggregate selected layers
            states = [out.hidden_states[l][:, 1:, :] for l in self.extract_layers]  # drop CLS
            feat = torch.stack(states, dim=0).mean(0)   # (B, N, 384)
        else:
            feat = self.model(x)                         # (B, 384, 14, 14)
            B, C, H, W = feat.shape
            feat = feat.permute(0, 2, 3, 1).reshape(B, H * W, C)  # (B, 196, 384)
        return feat
