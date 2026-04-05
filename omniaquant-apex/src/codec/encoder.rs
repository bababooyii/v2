//! OmniQuant-Apex Encoder — Algorithm 1
//!
//! For each frame F_t:
//! 1. Extract z_t = ULEP.encode(F_t)
//! 2. Decide if keyframe (periodic or LCC-triggered or first frame)
//! 3a. Keyframe:   GTM.encode(z_t) → KeyframePacket
//! 3b. Predictive: ẑ_t = ULEP.predict(), Δz_t = z_t - ẑ_t
//!                 Sparse-select top-k of Δz_t
//!                 GTM.encode(sparse Δz_t) → PredictivePacket
//! 4. Yield serialized packet bytes + per-frame metadata

use image::DynamicImage;
use ndarray::Array1;
use serde::{Deserialize, Serialize};

use crate::codec::lcc::{LCCMethod, LCC};
use crate::codec::packets::{KeyframePacket, Packet, PredictivePacket};
use crate::codec::sparse::SparseCoder;
use crate::gtm::GTMEncoder;
use crate::ulep::ULEP;

/// Per-frame encoding statistics.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct EncoderStats {
    pub frame_idx: usize,
    pub is_keyframe: bool,
    pub lcc_triggered: bool,
    pub prediction_diverged: bool,
    pub packet_bytes: usize,
    pub delta_z_norm: f64,
    pub energy_retention: f64,
    pub sparse_k: usize,
}

/// OmniQuant-Apex streaming encoder.
pub struct OmniQuantEncoder {
    ulep: ULEP,
    latent_dim: usize,
    keyframe_interval: usize,
    gtm_enc_kf: GTMEncoder,
    gtm_enc_pf: GTMEncoder,
    sparse_coder: SparseCoder,
    lcc: LCC,
    frame_idx: usize,
}

impl OmniQuantEncoder {
    pub fn new(
        ulep: ULEP,
        latent_dim: usize,
        keyframe_interval: usize,
        lcc_threshold: f64,
        sparse_fraction: f64,
        gtm_bits_keyframe: usize,
        gtm_bits_predictive: usize,
        qjl_proj_dim: usize,
        seed: u64,
    ) -> Self {
        let gtm_enc_kf = GTMEncoder::new(gtm_bits_keyframe, qjl_proj_dim, seed);
        let gtm_enc_pf = GTMEncoder::new(gtm_bits_predictive, qjl_proj_dim, seed.wrapping_add(100));
        let sparse_coder = SparseCoder::new(sparse_fraction, 8);
        let lcc = LCC::new(lcc_threshold, LCCMethod::L2, 5);

        Self {
            ulep,
            latent_dim,
            keyframe_interval,
            gtm_enc_kf,
            gtm_enc_pf,
            sparse_coder,
            lcc,
            frame_idx: 0,
        }
    }

    /// Encode a single video frame.
    ///
    /// Returns: (Packet, EncoderStats)
    pub fn encode_frame(&mut self, frame: &DynamicImage) -> (Packet, EncoderStats) {
        let t = self.frame_idx;
        self.frame_idx += 1;

        // Step 1: Extract current latent
        let z_t = self.ulep.encode(frame);

        let mut lcc_triggered = false;
        let mut prediction_diverged = false;
        let mut is_keyframe = false;
        let mut delta_z_norm = 0.0;
        let mut energy_retention = 1.0;
        let mut sparse_k = self.latent_dim;

        // Step 2: Keyframe decision
        if t == 0 || (t % self.keyframe_interval == 0) {
            is_keyframe = true;
        } else {
            // Predictive: compute residual and run LCC
            if let Some(z_hat_t) = self.ulep.predict() {
                let delta_z = &z_t - &z_hat_t;

                // LCC check: preview quantization
                let delta_z_tilde = self.gtm_enc_pf.encode_decode(&delta_z);

                lcc_triggered = self.lcc.check(&delta_z, &delta_z_tilde, Some(&z_t));
                prediction_diverged = self.lcc.check_prediction_divergence(&z_t, &z_hat_t);

                delta_z_norm = delta_z.mapv(|x| x * x).sum().sqrt();

                if lcc_triggered || prediction_diverged {
                    is_keyframe = true;
                }
            } else {
                // Not enough history yet
                is_keyframe = true;
            }
        }

        // Step 3: Quantize and packetize
        let packet = if is_keyframe {
            let gtm_packets = self.gtm_enc_kf.encode(&z_t);
            let pkt = KeyframePacket::new(t, self.latent_dim, gtm_packets);
            Packet::Keyframe(pkt)
        } else {
            let z_hat_t = self
                .ulep
                .predict()
                .unwrap_or_else(|| Array1::zeros(self.latent_dim));
            let delta_z = &z_t - &z_hat_t;

            let k = self.sparse_coder.get_k(self.latent_dim);
            let (indices, values) = self.sparse_coder.encode_sparse(&delta_z, Some(k));
            energy_retention = self.sparse_coder.energy_retention(&delta_z, Some(k));
            sparse_k = k;

            let gtm_packets = self.gtm_enc_pf.encode(&values);
            let pkt = PredictivePacket::new(t, self.latent_dim, k, indices, gtm_packets);
            Packet::Predictive(pkt)
        };

        let packet_bytes = packet.size_bytes();

        // Step 4: Update ULEP state
        self.ulep.update_state(z_t);

        let stats = EncoderStats {
            frame_idx: t,
            is_keyframe,
            lcc_triggered,
            prediction_diverged,
            packet_bytes,
            delta_z_norm,
            energy_retention,
            sparse_k,
        };

        (packet, stats)
    }

    /// Reset encoder state for a new video sequence.
    pub fn reset(&mut self) {
        self.frame_idx = 0;
        self.ulep.reset_state();
        self.lcc.reset();
    }

    pub fn set_lcc_threshold(&mut self, threshold: f64) {
        self.lcc.set_threshold(threshold);
    }

    pub fn set_sparse_fraction(&mut self, fraction: f64) {
        self.sparse_coder.set_top_k_fraction(fraction);
    }
}
