//! OmniQuant-Apex Decoder — Algorithm 2
//!
//! For each received packet:
//! 1. Deserialize → KeyframePacket or PredictivePacket
//! 2. Reconstruct z_t:
//!    - Keyframe:    GTM.decode(full quantized z_t)
//!    - Predictive:  ẑ_t = ULEP.predict(); Δz_t = GTM.decode(sparse); z_t = ẑ_t + Δz_t
//! 3. Generate frame: F̂_t = MR-GWD.synthesize(z_t)
//! 4. Error concealment on missing packets (predict from state)

use image::{DynamicImage, GenericImageView};
use ndarray::Array1;
use serde::{Deserialize, Serialize};

use crate::codec::packets::Packet;
use crate::codec::sparse::SparseCoder;
use crate::gtm::GTMDecoder;
use crate::mrgwd::MRGWD;
use crate::ulep::ULEP;

/// Per-frame decoding statistics.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct DecoderStats {
    pub frame_idx: usize,
    pub packet_type: String,
    pub decoded_frame_size: (u32, u32),
    pub z_t_norm: f64,
}

/// OmniQuant-Apex streaming decoder.
pub struct OmniQuantDecoder {
    ulep: ULEP,
    mrgwd: MRGWD,
    latent_dim: usize,
    gtm_decoder: GTMDecoder,
    sparse_coder: SparseCoder,
    expected_frame_idx: usize,
}

impl OmniQuantDecoder {
    pub fn new(
        ulep: ULEP,
        mrgwd: MRGWD,
        latent_dim: usize,
        qjl_proj_dim: usize,
        sparse_fraction: f64,
    ) -> Self {
        Self {
            ulep,
            mrgwd,
            latent_dim,
            gtm_decoder: GTMDecoder::new(qjl_proj_dim),
            sparse_coder: SparseCoder::new(sparse_fraction, 8),
            expected_frame_idx: 0,
        }
    }

    /// Decode a single serialized packet into a frame.
    ///
    /// Returns: (frame_image, DecoderStats)
    pub fn decode_packet(&mut self, packet_bytes: &[u8]) -> Option<(DynamicImage, DecoderStats)> {
        let packet = Packet::from_bytes(packet_bytes).ok()?;
        self.process_packet(&packet)
    }

    /// Decode from an already-deserialized packet object.
    pub fn decode_packet_object(
        &mut self,
        packet: &Packet,
    ) -> Option<(DynamicImage, DecoderStats)> {
        self.process_packet(packet)
    }

    fn process_packet(&mut self, packet: &Packet) -> Option<(DynamicImage, DecoderStats)> {
        // Handle missing frames (error concealment)
        if let Packet::Keyframe(kf) = packet {
            if kf.frame_idx > self.expected_frame_idx {
                self.conceal_frames(kf.frame_idx - self.expected_frame_idx);
            }
        }

        // Reconstruct z_t
        let z_t = match packet {
            Packet::Keyframe(kf) => self.gtm_decoder.decode(&kf.gtm_packets, kf.latent_dim),
            Packet::Predictive(pf) => {
                let z_hat_t = self
                    .ulep
                    .predict()
                    .unwrap_or_else(|| Array1::zeros(pf.latent_dim));

                // Decode sparse delta from GTM packets
                let quant_values = self.gtm_decoder.decode(&pf.gtm_packets, pf.top_k);

                // Reconstruct dense delta_z
                let delta_z =
                    self.sparse_coder
                        .decode_sparse(&pf.indices, &quant_values, pf.latent_dim);

                z_hat_t + delta_z
            }
        };

        let z_norm = z_t.mapv(|x| x * x).sum().sqrt();

        // Synthesize frame
        let frame = self.mrgwd.synthesize(&z_t);
        let frame_size = frame.dimensions();

        // Update state
        self.ulep.update_state(z_t);
        self.expected_frame_idx = packet.frame_idx() + 1;

        let pkt_type = match packet {
            Packet::Keyframe(_) => "keyframe",
            Packet::Predictive(_) => "predictive",
        };

        Some((
            frame,
            DecoderStats {
                frame_idx: packet.frame_idx(),
                packet_type: pkt_type.to_string(),
                decoded_frame_size: frame_size,
                z_t_norm: z_norm,
            },
        ))
    }

    /// Error concealment for lost packets.
    fn conceal_frames(&mut self, n_missing: usize) {
        for _ in 0..n_missing {
            let z_pred = self
                .ulep
                .predict()
                .unwrap_or_else(|| Array1::zeros(self.latent_dim));
            let _ = self.mrgwd.synthesize(&z_pred);
            self.ulep.update_state(z_pred);
            self.expected_frame_idx += 1;
        }
    }

    /// Publicly exposed error concealment: call when a packet is missing.
    pub fn conceal_one_frame(&mut self) -> Option<(DynamicImage, DecoderStats)> {
        let z_pred = self
            .ulep
            .predict()
            .unwrap_or_else(|| Array1::zeros(self.latent_dim));

        let z_norm = z_pred.mapv(|x| x * x).sum().sqrt();
        let frame = self.mrgwd.synthesize(&z_pred);
        let frame_size = frame.dimensions();

        self.ulep.update_state(z_pred);
        let frame_idx = self.expected_frame_idx;
        self.expected_frame_idx += 1;

        Some((
            frame,
            DecoderStats {
                frame_idx,
                packet_type: "concealed".to_string(),
                decoded_frame_size: frame_size,
                z_t_norm: z_norm,
            },
        ))
    }

    /// Reset decoder state.
    pub fn reset(&mut self) {
        self.expected_frame_idx = 0;
        self.ulep.reset_state();
        self.mrgwd.reset_state();
    }
}
