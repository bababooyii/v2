"""
GTM Codec — Orchestrates the full GTM encode/decode pipeline:
RHT → Polar Transform → Lloyd-Max Quantization → QJL Bias Correction

Supports:
  - Full vector encoding (keyframe latents z_t)
  - Chunked processing for large D
  - Adaptive bit allocation (more bits for radius, fewer for angles)
  - Serializable GTMPackets for transmission over a network channel
"""
import torch
import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import io, struct

from .rht import RHT
from .polar import polar_transform, polar_inverse
from .quantize import LloydMaxQuantizer
from .qjl import QJL


@dataclass
class GTMPacket:
    """Serialisable container for a quantized latent chunk."""
    chunk_idx: int          # chunk index within full vector
    chunk_size: int         # original chunk dimensionality
    r_idx: int              # quantized radius index
    theta_indices: List[int]  # quantized angle indices
    qjl_bits: bytes         # packed QJL sign bits
    n_bits_r: int
    n_bits_theta: int
    padded_dim: int

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        # !HHHHHHi = 5*2 + 4 + 4 = 18 bytes total
        buf.write(struct.pack("!HHHHHHi", self.chunk_idx, self.chunk_size,
                              self.n_bits_r, self.n_bits_theta,
                              len(self.theta_indices), self.padded_dim, self.r_idx))
        for t in self.theta_indices:
            buf.write(struct.pack("!H", t))
        buf.write(struct.pack("!H", len(self.qjl_bits)))
        buf.write(self.qjl_bits)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "GTMPacket":
        buf = io.BytesIO(data)
        # !HHHHHHi = 16 bytes (5*H + I + i)
        header = buf.read(16)
        if len(header) < 16:
            raise ValueError(f"GTMPacket header incomplete: {len(header)}/16 bytes")
        chunk_idx, chunk_size, n_bits_r, n_bits_theta, n_theta, padded_dim, r_idx = \
            struct.unpack("!HHHHHHi", header)
        theta_indices = []
        for _ in range(n_theta):
            t_bytes = buf.read(2)
            if len(t_bytes) < 2:
                raise ValueError("Incomplete theta index")
            theta_indices.append(struct.unpack("!H", t_bytes)[0])
        qjl_len_bytes = buf.read(2)
        if len(qjl_len_bytes) < 2:
            raise ValueError("Missing QJL length")
        qjl_len = struct.unpack("!H", qjl_len_bytes)[0]
        qjl_bits = buf.read(qjl_len)
        if len(qjl_bits) < qjl_len:
            raise ValueError(f"Incomplete QJL bits: {len(qjl_bits)}/{qjl_len}")
        return cls(chunk_idx=chunk_idx, chunk_size=chunk_size, r_idx=r_idx,
                   theta_indices=theta_indices, qjl_bits=qjl_bits,
                   n_bits_r=n_bits_r, n_bits_theta=n_bits_theta, padded_dim=padded_dim)


class GTMEncoder:
    """
    Applies RHT + polar quantization + QJL to a latent vector.
    Produces a list of GTMPacket objects (one per chunk).
    """

    CHUNK_SIZE = 32  # process in 32-dim chunks

    def __init__(self, n_bits: int = 4, qjl_proj_dim: int = 64,
                 n_bits_r: Optional[int] = None, seed: int = 42):
        self.n_bits = n_bits
        self.n_bits_r = n_bits_r or min(n_bits + 2, 8)  # radius gets extra bits
        self.n_bits_theta = n_bits
        self.qjl = QJL(proj_dim=qjl_proj_dim, seed=seed + 1)
        self._qz_r = LloydMaxQuantizer(self.n_bits_r).default_fit(scale=3.0)
        self._qz_theta = LloydMaxQuantizer(self.n_bits_theta).default_fit(scale=math.pi / 2)
        self._rhts: dict = {}  # chunk_size → RHT

    def _get_rht(self, dim: int, seed_offset: int) -> RHT:
        key = (dim, seed_offset)
        if key not in self._rhts:
            self._rhts[key] = RHT(dim, seed=42 + seed_offset)
        return self._rhts[key]

    def encode(self, v: torch.Tensor) -> List[GTMPacket]:
        """
        Encode 1-D latent vector v (shape: D,) into a list of GTMPackets.
        """
        packets = []
        D = v.shape[0]
        cs = self.CHUNK_SIZE
        n_chunks = math.ceil(D / cs)

        for i in range(n_chunks):
            chunk = v[i * cs: (i + 1) * cs]
            actual_size = chunk.shape[0]

            rht = self._get_rht(actual_size, i)
            v_rht = rht.forward(chunk)                       # (padded_dim,)

            # Polar decompose
            r, thetas = polar_transform(v_rht.unsqueeze(0))  # (1,), (1, pd-1)
            r = r.squeeze(0)
            thetas = thetas.squeeze(0)

            # Quantize radius
            r_idx = int(self._qz_r.quantize(r.unsqueeze(0)).item())

            # Quantize angles
            theta_indices = self._qz_theta.quantize(thetas).tolist()

            # Reconstruct to compute residual for QJL
            r_rec = self._qz_r.dequantize(torch.tensor([r_idx]))
            theta_rec = self._qz_theta.dequantize(torch.tensor(theta_indices))
            v_rec = polar_inverse(r_rec, theta_rec.unsqueeze(0)).squeeze(0)
            v_original_back = rht.inverse(v_rht.unsqueeze(0) if False else v_rec.unsqueeze(0)).squeeze(0)
            residual = chunk - rht.inverse(v_rec.unsqueeze(0)).squeeze(0)

            # QJL encode residual
            qjl_signs = self.qjl.encode(residual)           # (proj_dim,) bool
            qjl_bytes = _pack_bits(qjl_signs)

            packets.append(GTMPacket(
                chunk_idx=i, chunk_size=actual_size,
                r_idx=r_idx, theta_indices=[int(t) for t in theta_indices],
                qjl_bits=qjl_bytes,
                n_bits_r=self.n_bits_r, n_bits_theta=self.n_bits_theta,
                padded_dim=rht.padded_dim,
            ))

        return packets

    def encode_decode(self, v: torch.Tensor) -> torch.Tensor:
        """Encode then immediately decode — used for LCC preview."""
        packets = self.encode(v)
        return GTMDecoder(qjl_proj_dim=self.qjl.proj_dim).decode(packets, v.shape[0])


class GTMDecoder:
    """Reconstructs a latent vector from a list of GTMPackets."""

    def __init__(self, qjl_proj_dim: int = 64, seed: int = 42):
        self.qjl = QJL(proj_dim=qjl_proj_dim, seed=seed + 1)
        self._rhts: dict = {}
        self._qz_cache: dict = {}

    def _get_rht(self, dim: int, seed_offset: int) -> RHT:
        key = (dim, seed_offset)
        if key not in self._rhts:
            self._rhts[key] = RHT(dim, seed=42 + seed_offset)
        return self._rhts[key]

    def _get_qz(self, n_bits: int) -> LloydMaxQuantizer:
        if n_bits not in self._qz_cache:
            self._qz_cache[n_bits] = LloydMaxQuantizer(n_bits).default_fit(scale=math.pi / 2)
        return self._qz_cache[n_bits]

    def decode(self, packets: List[GTMPacket], orig_dim: int) -> torch.Tensor:
        """Reconstruct latent vector from packets."""
        cs = 32
        out = torch.zeros(orig_dim)

        for pkt in sorted(packets, key=lambda p: p.chunk_idx):
            i = pkt.chunk_idx
            rht = self._get_rht(pkt.chunk_size, i)
            qz_r = LloydMaxQuantizer(pkt.n_bits_r).default_fit(scale=3.0)
            qz_theta = self._get_qz(pkt.n_bits_theta)

            r_rec = qz_r.dequantize(torch.tensor([pkt.r_idx]))
            theta_rec = qz_theta.dequantize(torch.tensor(pkt.theta_indices))
            v_polar = polar_inverse(r_rec, theta_rec.unsqueeze(0)).squeeze(0)
            v_chunk = rht.inverse(v_polar.unsqueeze(0)).squeeze(0)

            # QJL correction
            qjl_signs = _unpack_bits(pkt.qjl_bits, self.qjl.proj_dim)
            correction = self.qjl.decode(qjl_signs, pkt.chunk_size)
            v_chunk = v_chunk + correction

            start = i * cs
            end = start + pkt.chunk_size
            out[start:end] = v_chunk

        return out


# -------------------------------------------------------------------------
# Bit packing utilities
# -------------------------------------------------------------------------

def _pack_bits(bool_tensor: torch.Tensor) -> bytes:
    """Pack a bool tensor into bytes."""
    flat = bool_tensor.flatten().tolist()
    n = len(flat)
    n_bytes = math.ceil(n / 8)
    ba = bytearray(n_bytes)
    for i, b in enumerate(flat):
        if b:
            ba[i // 8] |= (1 << (i % 8))
    return bytes(ba)


def _unpack_bits(data: bytes, n_bits: int) -> torch.Tensor:
    """Unpack bytes back to bool tensor of length n_bits."""
    out = []
    for i in range(n_bits):
        byte_idx = i // 8
        bit_idx = i % 8
        if byte_idx < len(data):
            out.append(bool((data[byte_idx] >> bit_idx) & 1))
        else:
            out.append(False)
    return torch.tensor(out, dtype=torch.bool)
