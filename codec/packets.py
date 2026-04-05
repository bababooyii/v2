"""
OmniQuant-Apex Bitstream Packet Format.

KeyframePacket:     carries full quantized z_t (recovered without prediction)
PredictivePacket:   carries sparse quantized Δz_t (applied on top of ẑ_t)

Serialisation uses a compact binary format (struct + msgpack-style).
"""
import struct
import io
from dataclasses import dataclass, field
from typing import List, Optional
from gtm.codec import GTMPacket


@dataclass
class KeyframePacket:
    """Full keyframe with quantized z_t."""
    frame_idx: int
    latent_dim: int
    gtm_packets: List[GTMPacket]         # one per chunk

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        buf.write(b"KF")
        buf.write(struct.pack("!IH", self.frame_idx, self.latent_dim))
        buf.write(struct.pack("!H", len(self.gtm_packets)))
        for pkt in self.gtm_packets:
            raw = pkt.to_bytes()
            buf.write(struct.pack("!I", len(raw)))
            buf.write(raw)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "KeyframePacket":
        buf = io.BytesIO(data)
        assert buf.read(2) == b"KF"
        frame_idx, latent_dim = struct.unpack("!IH", buf.read(6))
        n_pkts = struct.unpack("!H", buf.read(2))[0]
        gtm_packets = []
        for _ in range(n_pkts):
            pkt_len = struct.unpack("!I", buf.read(4))[0]
            gtm_packets.append(GTMPacket.from_bytes(buf.read(pkt_len)))
        return cls(frame_idx=frame_idx, latent_dim=latent_dim, gtm_packets=gtm_packets)


@dataclass
class PredictivePacket:
    """Predictive frame with sparse quantized Δz_t."""
    frame_idx: int
    latent_dim: int
    top_k: int                           # number of transmitted components
    indices: List[int]                   # which dimensions (sparse)
    gtm_packets: List[GTMPacket]         # GTM-quantized sparse values

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        buf.write(b"PF")
        buf.write(struct.pack("!IHH", self.frame_idx, self.latent_dim, self.top_k))
        buf.write(struct.pack("!H", len(self.indices)))
        for idx in self.indices:
            buf.write(struct.pack("!H", idx))
        buf.write(struct.pack("!H", len(self.gtm_packets)))
        for pkt in self.gtm_packets:
            raw = pkt.to_bytes()
            buf.write(struct.pack("!I", len(raw)))
            buf.write(raw)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, data: bytes) -> "PredictivePacket":
        buf = io.BytesIO(data)
        assert buf.read(2) == b"PF"
        frame_idx, latent_dim, top_k = struct.unpack("!IHH", buf.read(8))
        n_idx = struct.unpack("!H", buf.read(2))[0]
        indices = [struct.unpack("!H", buf.read(2))[0] for _ in range(n_idx)]
        n_pkts = struct.unpack("!H", buf.read(2))[0]
        gtm_packets = []
        for _ in range(n_pkts):
            pkt_len = struct.unpack("!I", buf.read(4))[0]
            gtm_packets.append(GTMPacket.from_bytes(buf.read(pkt_len)))
        return cls(frame_idx=frame_idx, latent_dim=latent_dim, top_k=top_k,
                   indices=indices, gtm_packets=gtm_packets)


def serialize_packet(pkt) -> bytes:
    """Serialize either packet type with a 1-byte type prefix."""
    if isinstance(pkt, KeyframePacket):
        data = pkt.to_bytes()
        return b"\x01" + struct.pack("!I", len(data)) + data
    elif isinstance(pkt, PredictivePacket):
        data = pkt.to_bytes()
        return b"\x02" + struct.pack("!I", len(data)) + data
    raise TypeError(f"Unknown packet type: {type(pkt)}")


def deserialize_packet(data: bytes):
    """Deserialize a packet from bytes."""
    pkt_type = data[0:1]
    pkt_len = struct.unpack("!I", data[1:5])[0]
    payload = data[5:5 + pkt_len]
    if pkt_type == b"\x01":
        return KeyframePacket.from_bytes(payload)
    elif pkt_type == b"\x02":
        return PredictivePacket.from_bytes(payload)
    raise ValueError(f"Unknown packet type byte: {pkt_type}")
