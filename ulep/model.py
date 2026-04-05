"""
ULEP — Unified Latent Encoder-Predictor

Composes ULEPBackbone + EncodeHead + PredictorHead into a single streaming model
with built-in state management for the last 2 latent states.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from pathlib import Path
from typing import Optional, Tuple, List
from PIL import Image

def _get_transform():
    """Lazy import to avoid torchvision issues on Python 3.14 + torch CPU."""
    import torchvision.transforms as T
    return T.Compose([
        T.Resize((224, 224)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

from .backbone import ULEPBackbone
from .encode_head import EncodeHead
from .predictor_head import PredictorHead


class ULEP(nn.Module):
    """
    Unified Latent Encoder-Predictor.

    Usage (streaming):
        ulep = ULEP(latent_dim=512)
        ulep.reset_state()
        for frame in video:
            z_t = ulep.encode(frame)
            z_hat_t = ulep.predict()        # uses internal state
            delta_z = z_t - z_hat_t
            ulep.update_state(z_t)
    """

    # ImageNet normalisation for DINOv2
    # Lazy-loaded via _get_transform() to avoid torchvision import at module level

    def __init__(self, latent_dim: int = 512, feat_dim: int = 384,
                 hidden_dim: int = 1024, use_pretrained: bool = True):
        super().__init__()
        self.latent_dim = latent_dim
        self.feat_dim = feat_dim

        self.backbone = ULEPBackbone(use_pretrained=use_pretrained)
        self.encode_head = EncodeHead(feat_dim=feat_dim, latent_dim=latent_dim,
                                      hidden_dim=hidden_dim)
        self.predictor_head = PredictorHead(latent_dim=latent_dim,
                                            hidden_dim=hidden_dim)

        # Streaming state
        self._z_t_minus_1: Optional[torch.Tensor] = None
        self._z_t_minus_2: Optional[torch.Tensor] = None

    # ------------------------------------------------------------------
    # Encode
    # ------------------------------------------------------------------

    @torch.no_grad()
    def encode(self, frame) -> torch.Tensor:
        """
        Encode a single frame to latent z_t.

        frame: PIL.Image, torch.Tensor (3,H,W) or (1,3,H,W)
        returns: (latent_dim,) tensor
        """
        x = self._preprocess(frame)              # (1, 3, 224, 224)
        features = self.backbone(x)              # (1, N, feat_dim)
        z = self.encode_head(features)           # (1, latent_dim)
        return z.squeeze(0)

    def encode_trainable(self, frame) -> torch.Tensor:
        """Encode with gradients (for training)."""
        x = self._preprocess(frame)
        features = self.backbone(x)
        z = self.encode_head(features)
        return z.squeeze(0)

    # ------------------------------------------------------------------
    # Predict
    # ------------------------------------------------------------------

    @torch.no_grad()
    def predict(self, z_prev: Optional[torch.Tensor] = None,
                z_prev2: Optional[torch.Tensor] = None) -> Optional[torch.Tensor]:
        """
        Predict ẑ_t from previous latents.
        Uses internal state if arguments not provided.
        Returns None if not enough history (< 2 frames seen).
        """
        z1 = z_prev if z_prev is not None else self._z_t_minus_1
        z2 = z_prev2 if z_prev2 is not None else self._z_t_minus_2

        if z1 is None or z2 is None:
            return z1  # best we can do: repeat last

        return self.predictor_head(z1, z2)

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def update_state(self, z_t: torch.Tensor):
        """Call after each frame to advance the internal state buffer."""
        self._z_t_minus_2 = self._z_t_minus_1
        self._z_t_minus_1 = z_t.detach()

    def reset_state(self):
        """Reset streaming state (call at start of new video sequence)."""
        self._z_t_minus_1 = None
        self._z_t_minus_2 = None

    def has_enough_history(self) -> bool:
        return self._z_t_minus_1 is not None and self._z_t_minus_2 is not None

    # ------------------------------------------------------------------
    # Preprocessing
    # ------------------------------------------------------------------

    def _preprocess(self, frame) -> torch.Tensor:
        if isinstance(frame, Image.Image):
            return _get_transform()(frame).unsqueeze(0)
        elif isinstance(frame, torch.Tensor):
            if frame.dim() == 3:
                frame = frame.unsqueeze(0)
            # Assume (1, 3, H, W) already tensor — resize if needed
            if frame.shape[-2:] != (224, 224):
                frame = F.interpolate(frame.float(), size=(224, 224), mode="bilinear",
                                      align_corners=False)
            return frame
        else:
            raise TypeError(f"Unsupported frame type: {type(frame)}")

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def save(self, path: str):
        """Save trainable heads (backbone stays frozen / separate)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "encode_head": self.encode_head.state_dict(),
            "predictor_head": self.predictor_head.state_dict(),
            "latent_dim": self.latent_dim,
            "feat_dim": self.feat_dim,
        }, p)

    def load(self, path: str):
        ckpt = torch.load(path, map_location="cpu")
        self.encode_head.load_state_dict(ckpt["encode_head"])
        self.predictor_head.load_state_dict(ckpt["predictor_head"])
        return self

    def to(self, device):
        super().to(device)
        self.backbone.to(device)
        return self
