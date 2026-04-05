//! Packet types for the OmniQuant-Apex bitstream.
//!
//! KeyframePacket: carries full quantized z_t
//! PredictivePacket: carries sparse quantized Δz_t

use crate::gtm::GTMPacket;
use serde::{Deserialize, Serialize};

/// Full keyframe with quantized z_t.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct KeyframePacket {
    pub frame_idx: usize,
    pub latent_dim: usize,
    pub gtm_packets: Vec<GTMPacket>,
}

/// Predictive frame with sparse quantized Δz_t.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct PredictivePacket {
    pub frame_idx: usize,
    pub latent_dim: usize,
    pub top_k: usize,
    pub indices: Vec<u16>,
    pub gtm_packets: Vec<GTMPacket>,
}

/// Unified packet type for serialization.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub enum Packet {
    Keyframe(KeyframePacket),
    Predictive(PredictivePacket),
}

impl Packet {
    pub fn frame_idx(&self) -> usize {
        match self {
            Packet::Keyframe(kf) => kf.frame_idx,
            Packet::Predictive(pf) => pf.frame_idx,
        }
    }

    pub fn is_keyframe(&self) -> bool {
        matches!(self, Packet::Keyframe(_))
    }

    /// Serialize to JSON bytes (for WebSocket transport).
    pub fn to_bytes(&self) -> Vec<u8> {
        serde_json::to_vec(self).expect("Failed to serialize packet")
    }

    /// Deserialize from JSON bytes.
    pub fn from_bytes(data: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(data)
    }

    /// Estimate serialized size in bytes.
    pub fn size_bytes(&self) -> usize {
        self.to_bytes().len()
    }
}

impl KeyframePacket {
    pub fn new(frame_idx: usize, latent_dim: usize, gtm_packets: Vec<GTMPacket>) -> Self {
        Self {
            frame_idx,
            latent_dim,
            gtm_packets,
        }
    }
}

impl PredictivePacket {
    pub fn new(
        frame_idx: usize,
        latent_dim: usize,
        top_k: usize,
        indices: Vec<u16>,
        gtm_packets: Vec<GTMPacket>,
    ) -> Self {
        Self {
            frame_idx,
            latent_dim,
            top_k,
            indices,
            gtm_packets,
        }
    }
}
