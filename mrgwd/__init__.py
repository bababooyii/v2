"""
Multi-Resolution Generative World Decoder (MR-GWD)
Synthesizes high-resolution frames from latent states z_t
using a latent diffusion synthesis stage and a temporal upsampler.
"""
from .model import MRGWD
__all__ = ["MRGWD"]
