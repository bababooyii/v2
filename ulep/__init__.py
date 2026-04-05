"""
Unified Latent Encoder-Predictor (ULEP)
Encodes video frames into compact object-centric latent states z_t
and predicts temporal evolution ẑ_t using a GRU-based predictor.
"""
from .model import ULEP
__all__ = ["ULEP"]
