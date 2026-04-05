"""
OmniQuant-Apex Decoder — Algorithm 2

For each received packet:
1.  Deserialize → KeyframePacket or PredictivePacket
2.  Reconstruct z_t:
    - Keyframe:    GTM.decode(full quantized z_t)
    - Predictive:  ẑ_t = ULEP.predict(); Δz_t = GTM.decode(sparse); z_t = ẑ_t + Δz_t
3.  Generate frame: F̂_t = MR-GWD.synthesize(z_t)
4.  Error concealment on missing packets (predict from state)
"""
import torch
from typing import Optional, Tuple
from dataclasses import dataclass

from ulep.model import ULEP
from gtm.codec import GTMDecoder as GTMDec
from mrgwd.model import MRGWD
from codec.packets import KeyframePacket, PredictivePacket, deserialize_packet
from codec.sparse import SparseCoder


@dataclass
class DecoderStats:
    frame_idx: int
    packet_type: str   # "keyframe", "predictive", "concealed"
    decoded_frame_shape: tuple
    z_t_norm: float


class OmniQuantDecoder:
    """
    OmniQuant-Apex streaming decoder.

    Feed serialized packet bytes one at a time via decode_packet().
    Each call returns a synthesized frame tensor (3, H, W) in [-1, 1].
    """

    def __init__(
        self,
        ulep: ULEP,
        mrgwd: MRGWD,
        latent_dim: int = 512,
        qjl_proj_dim: int = 64,
        sparse_fraction: float = 0.25,
    ):
        self.ulep = ulep
        self.mrgwd = mrgwd
        self.latent_dim = latent_dim

        self.gtm_decoder = GTMDec(qjl_proj_dim=qjl_proj_dim)
        self.sparse_coder = SparseCoder(top_k_fraction=sparse_fraction)

        self.ulep.reset_state()
        self.mrgwd.reset_state()

        self._expected_frame_idx: int = 0

    def decode_packet(self, packet_bytes: bytes) -> Tuple[torch.Tensor, DecoderStats]:
        """
        Decode a single serialized packet into a frame.

        Returns: (frame_tensor: (3,H,W) in [-1,1], DecoderStats)
        """
        pkt = deserialize_packet(packet_bytes)
        return self._process_packet(pkt)

    def decode_packet_object(self, pkt) -> Tuple[torch.Tensor, DecoderStats]:
        """Decode from already-deserialized packet object."""
        return self._process_packet(pkt)

    def _process_packet(self, pkt) -> Tuple[torch.Tensor, DecoderStats]:
        # Handle missing frames (error concealment)
        if isinstance(pkt, KeyframePacket):
            if pkt.frame_idx > self._expected_frame_idx:
                self._conceal_frames(pkt.frame_idx - self._expected_frame_idx)

        # Reconstruct z_t
        if isinstance(pkt, KeyframePacket):
            z_t = self.gtm_decoder.decode(pkt.gtm_packets, pkt.latent_dim)
            pkt_type = "keyframe"
        elif isinstance(pkt, PredictivePacket):
            z_hat_t = self.ulep.predict()
            if z_hat_t is None:
                z_hat_t = torch.zeros(pkt.latent_dim)

            # Decode sparse delta from GTM packets
            # The GTM packets encode the values vector (length = top_k)
            quant_values = self.gtm_decoder.decode(pkt.gtm_packets, pkt.top_k)

            # Reconstruct dense delta_z
            indices = torch.tensor(pkt.indices, dtype=torch.long)
            delta_z = self.sparse_coder.decode_sparse(indices, quant_values, pkt.latent_dim)

            z_t = z_hat_t + delta_z
            pkt_type = "predictive"
        else:
            raise TypeError(f"Unknown packet type: {type(pkt)}")

        # Synthesize frame
        frame = self.mrgwd.synthesize(z_t)

        # Update state
        self.ulep.update_state(z_t)
        self._expected_frame_idx = pkt.frame_idx + 1

        return frame, DecoderStats(
            frame_idx=pkt.frame_idx,
            packet_type=pkt_type,
            decoded_frame_shape=tuple(frame.shape),
            z_t_norm=float(z_t.norm()),
        )

    def _conceal_frames(self, n_missing: int):
        """
        Error concealment for lost packets:
        Predict z_t from state and synthesize via MR-GWD.
        State is updated as if these frames were decoded normally.
        """
        for i in range(n_missing):
            z_pred = self.ulep.predict()
            if z_pred is None:
                z_pred = torch.zeros(self.latent_dim)
            # Synthesize but discard (just updating state for consistency)
            _ = self.mrgwd.synthesize(z_pred)
            self.ulep.update_state(z_pred)
            self._expected_frame_idx += 1

    def conceal_one_frame(self) -> Tuple[torch.Tensor, DecoderStats]:
        """
        Publicly exposed error concealment: call when a packet is missing.
        Returns the concealed frame.
        """
        z_pred = self.ulep.predict()
        if z_pred is None:
            z_pred = torch.zeros(self.latent_dim)

        frame = self.mrgwd.synthesize(z_pred)
        self.ulep.update_state(z_pred)

        return frame, DecoderStats(
            frame_idx=self._expected_frame_idx,
            packet_type="concealed",
            decoded_frame_shape=tuple(frame.shape),
            z_t_norm=float(z_pred.norm()),
        )

    def reset(self):
        self._expected_frame_idx = 0
        self.ulep.reset_state()
        self.mrgwd.reset_state()
