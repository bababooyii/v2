"""
OmniQuant-Apex Encoder — Algorithm 1

For each frame F_t:
1.  Extract z_t = ULEP.encode(F_t)
2.  Decide if keyframe (periodic or LCC-triggered or first frame)
3a. Keyframe:   GTM.encode(z_t) → KeyframePacket
3b. Predictive: ẑ_t = ULEP.predict(), Δz_t = z_t - ẑ_t
                Sparse-select top-k of Δz_t
                GTM.encode(sparse Δz_t) → PredictivePacket
4.  Yield serialized packet bytes + per-frame metadata
"""
import torch
from typing import Generator, Tuple, Optional, Dict, Any
from dataclasses import dataclass

from ulep.model import ULEP
from gtm.codec import GTMEncoder
from codec.packets import KeyframePacket, PredictivePacket, serialize_packet
from codec.sparse import SparseCoder
from codec.lcc import LCC


@dataclass
class EncoderStats:
    frame_idx: int
    is_keyframe: bool
    lcc_triggered: bool
    prediction_diverged: bool
    packet_bytes: int
    delta_z_norm: float
    energy_retention: float
    sparse_k: int


class OmniQuantEncoder:
    """
    OmniQuant-Apex streaming encoder.

    Call encode_frame() for each frame in order.
    Returns serialized packet bytes + per-frame stats.
    """

    def __init__(
        self,
        ulep: ULEP,
        latent_dim: int = 512,
        keyframe_interval: int = 60,
        lcc_threshold: float = 0.15,
        lcc_method: str = "l2",
        gtm_bits_keyframe: int = 6,
        gtm_bits_predictive: int = 3,
        sparse_fraction: float = 0.25,
        qjl_proj_dim: int = 64,
    ):
        self.ulep = ulep
        self.latent_dim = latent_dim
        self.keyframe_interval = keyframe_interval

        self.gtm_enc_kf = GTMEncoder(n_bits=gtm_bits_keyframe, qjl_proj_dim=qjl_proj_dim)
        self.gtm_enc_pf = GTMEncoder(n_bits=gtm_bits_predictive, qjl_proj_dim=qjl_proj_dim)
        self.sparse_coder = SparseCoder(top_k_fraction=sparse_fraction)
        self.lcc = LCC(threshold=lcc_threshold, method=lcc_method)

        self._frame_idx: int = 0
        self.ulep.reset_state()

    def encode_frame(self, frame) -> Tuple[bytes, EncoderStats]:
        """
        Encode a single video frame.

        frame: PIL.Image or torch.Tensor
        returns: (serialized_bytes, EncoderStats)
        """
        t = self._frame_idx
        self._frame_idx += 1

        # Step 1: Extract current latent
        z_t = self.ulep.encode(frame)    # (D,)

        lcc_triggered = False
        prediction_diverged = False
        is_keyframe = False

        # Step 2: Keyframe decision
        if t == 0 or (t % self.keyframe_interval == 0):
            is_keyframe = True
        else:
            # Predictive: compute residual and run LCC
            z_hat_t = self.ulep.predict()

            if z_hat_t is None:
                # Not enough history yet
                is_keyframe = True
            else:
                delta_z = z_t - z_hat_t

                # LCC check: preview quantization
                delta_z_tilde = self.gtm_enc_pf.encode_decode(delta_z)

                lcc_triggered = self.lcc.check(delta_z, delta_z_tilde, z_ref=z_t)
                prediction_diverged = self.lcc.check_prediction_divergence(z_t, z_hat_t)

                if lcc_triggered or prediction_diverged:
                    is_keyframe = True

        # Step 3: Quantize and packetize
        if is_keyframe:
            gtm_packets = self.gtm_enc_kf.encode(z_t)
            pkt = KeyframePacket(
                frame_idx=t,
                latent_dim=self.latent_dim,
                gtm_packets=gtm_packets,
            )
            serialized = serialize_packet(pkt)
            stats = EncoderStats(
                frame_idx=t,
                is_keyframe=True,
                lcc_triggered=lcc_triggered,
                prediction_diverged=prediction_diverged,
                packet_bytes=len(serialized),
                delta_z_norm=0.0,
                energy_retention=1.0,
                sparse_k=self.latent_dim,
            )
        else:
            # Recompute (z_hat_t and delta_z computed earlier in this branch)
            z_hat_t = self.ulep.predict()
            delta_z = z_t - z_hat_t

            k = self.sparse_coder.get_k(self.latent_dim)
            indices, values = self.sparse_coder.encode_sparse(delta_z, k=k)
            energy = self.sparse_coder.energy_retention(delta_z, k=k)

            # Quantize the sparse values via GTM
            # Pack sparse values into a compact vector for GTM
            gtm_packets = self.gtm_enc_pf.encode(values)

            pkt = PredictivePacket(
                frame_idx=t,
                latent_dim=self.latent_dim,
                top_k=k,
                indices=indices.tolist(),
                gtm_packets=gtm_packets,
            )
            serialized = serialize_packet(pkt)
            stats = EncoderStats(
                frame_idx=t,
                is_keyframe=False,
                lcc_triggered=False,
                prediction_diverged=False,
                packet_bytes=len(serialized),
                delta_z_norm=float(delta_z.norm()),
                energy_retention=energy,
                sparse_k=k,
            )

        # Step 4: Update ULEP state
        self.ulep.update_state(z_t)

        return serialized, stats

    def encode_video(self, frames) -> Generator[Tuple[bytes, EncoderStats], None, None]:
        """Generator that encodes an iterable of frames."""
        self.reset()
        for frame in frames:
            yield self.encode_frame(frame)

    def reset(self):
        """Reset encoder state for a new video sequence."""
        self._frame_idx = 0
        self.ulep.reset_state()
        self.lcc.reset()

    def set_lcc_threshold(self, threshold: float):
        self.lcc.set_threshold(threshold)

    def set_sparse_fraction(self, fraction: float):
        self.sparse_coder.top_k_fraction = fraction
