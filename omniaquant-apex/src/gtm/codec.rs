//! GTM Codec — Orchestrates the full GTM encode/decode pipeline:
//! RHT → Polar Transform → Lloyd-Max Quantization → QJL Bias Correction

use ndarray::Array1;
use serde::{Deserialize, Serialize};

use super::polar::{polar_inverse, polar_transform};
use super::qjl::QJL;
use super::quantize::LloydMaxQuantizer;
use super::rht::RHT;

const CHUNK_SIZE: usize = 32;

/// Serializable container for a quantized latent chunk.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct GTMPacket {
    pub chunk_idx: usize,
    pub chunk_size: usize,
    pub r_idx: u16,
    pub theta_indices: Vec<u16>,
    pub qjl_bits: Vec<bool>,
    pub n_bits_r: usize,
    pub n_bits_theta: usize,
    pub padded_dim: usize,
}

/// GTM Encoder: applies RHT + polar quantization + QJL to a latent vector.
pub struct GTMEncoder {
    n_bits: usize,
    n_bits_r: usize,
    n_bits_theta: usize,
    qjl: QJL,
    qz_r: LloydMaxQuantizer,
    qz_theta: LloydMaxQuantizer,
    rhts: Vec<(usize, u64, RHT)>,
}

impl GTMEncoder {
    pub fn new(n_bits: usize, qjl_proj_dim: usize, seed: u64) -> Self {
        let n_bits_r = std::cmp::min(n_bits + 2, 8);
        let n_bits_theta = n_bits;

        let mut qz_r = LloydMaxQuantizer::new(n_bits_r);
        qz_r.default_fit(3.0);

        let mut qz_theta = LloydMaxQuantizer::new(n_bits_theta);
        qz_theta.default_fit(std::f64::consts::FRAC_PI_2);

        Self {
            n_bits,
            n_bits_r,
            n_bits_theta,
            qjl: QJL::new(qjl_proj_dim, seed.wrapping_add(1)),
            qz_r,
            qz_theta,
            rhts: Vec::new(),
        }
    }

    fn get_rht(&mut self, dim: usize, seed_offset: usize) -> &RHT {
        let seed = 42u64.wrapping_add(seed_offset as u64);
        if !self.rhts.iter().any(|(d, s, _)| *d == dim && *s == seed) {
            self.rhts.push((dim, seed, RHT::new(dim, seed)));
        }
        let idx = self
            .rhts
            .iter()
            .position(|(d, s, _)| *d == dim && *s == seed)
            .unwrap();
        &self.rhts[idx].2
    }

    /// Encode a 1-D latent vector into a list of GTMPackets.
    pub fn encode(&mut self, v: &Array1<f64>) -> Vec<GTMPacket> {
        let d = v.len();
        let n_chunks = (d + CHUNK_SIZE - 1) / CHUNK_SIZE;
        let mut packets = Vec::with_capacity(n_chunks);

        for i in 0..n_chunks {
            let start = i * CHUNK_SIZE;
            let end = std::cmp::min(start + CHUNK_SIZE, d);
            let chunk = v.slice(ndarray::s![start..end]).to_owned();
            let actual_size = chunk.len();

            // Build RHT separately to avoid borrow conflicts
            let seed = 42u64.wrapping_add(i as u64);
            let rht = RHT::new(actual_size, seed);
            let v_rht = rht.forward(&chunk);

            // Polar decompose
            let (r, thetas) = polar_transform(v_rht.view());

            // Quantize radius
            let r_idx = self.qz_r.quantize(r);

            // Quantize angles
            let theta_indices: Vec<u16> =
                thetas.iter().map(|&t| self.qz_theta.quantize(t)).collect();

            // Reconstruct for QJL residual
            let r_rec = self.qz_r.dequantize(r_idx);
            let theta_rec: Array1<f64> = theta_indices
                .iter()
                .map(|&idx| self.qz_theta.dequantize(idx))
                .collect();
            let v_rec = polar_inverse(r_rec, &theta_rec);
            let v_inv = rht.inverse(&v_rec);
            let residual = &chunk - &v_inv;

            // QJL encode residual
            let qjl_signs = self.qjl.encode(&residual);

            packets.push(GTMPacket {
                chunk_idx: i,
                chunk_size: actual_size,
                r_idx,
                theta_indices,
                qjl_bits: qjl_signs,
                n_bits_r: self.n_bits_r,
                n_bits_theta: self.n_bits_theta,
                padded_dim: rht.padded_dim(),
            });
        }

        packets
    }

    /// Encode then immediately decode — used for LCC preview.
    pub fn encode_decode(&mut self, v: &Array1<f64>) -> Array1<f64> {
        let packets = self.encode(v);
        GTMDecoder::new(self.qjl.proj_dim()).decode(&packets, v.len())
    }
}

/// GTM Decoder: reconstructs a latent vector from GTMPackets.
pub struct GTMDecoder {
    qjl: QJL,
    rhts: Vec<(usize, u64, RHT)>,
    qz_cache: std::collections::HashMap<usize, LloydMaxQuantizer>,
}

impl GTMDecoder {
    pub fn new(qjl_proj_dim: usize) -> Self {
        Self {
            qjl: QJL::new(qjl_proj_dim, 0),
            rhts: Vec::new(),
            qz_cache: std::collections::HashMap::new(),
        }
    }

    fn get_rht(&mut self, dim: usize, seed_offset: usize) -> &RHT {
        let seed = 42u64.wrapping_add(seed_offset as u64);
        if !self.rhts.iter().any(|(d, s, _)| *d == dim && *s == seed) {
            self.rhts.push((dim, seed, RHT::new(dim, seed)));
        }
        let idx = self
            .rhts
            .iter()
            .position(|(d, s, _)| *d == dim && *s == seed)
            .unwrap();
        &self.rhts[idx].2
    }

    fn get_qz_theta(&mut self, n_bits: usize) -> &LloydMaxQuantizer {
        self.qz_cache.entry(n_bits).or_insert_with(|| {
            let mut qz = LloydMaxQuantizer::new(n_bits);
            qz.default_fit(std::f64::consts::FRAC_PI_2);
            qz
        })
    }

    /// Reconstruct latent vector from packets.
    pub fn decode(&mut self, packets: &[GTMPacket], orig_dim: usize) -> Array1<f64> {
        let mut out = Array1::zeros(orig_dim);
        let mut sorted = packets.to_vec();
        sorted.sort_by_key(|p| p.chunk_idx);

        for pkt in &sorted {
            let i = pkt.chunk_idx;

            // Get RHT parameters first (clone to avoid borrow conflict)
            let seed = 42u64.wrapping_add(i as u64);
            let rht = RHT::new(pkt.chunk_size, seed);

            let mut qz_r = LloydMaxQuantizer::new(pkt.n_bits_r);
            qz_r.default_fit(3.0);

            // Build theta quantizer
            let mut qz_theta = LloydMaxQuantizer::new(pkt.n_bits_theta);
            qz_theta.default_fit(std::f64::consts::FRAC_PI_2);

            let r_rec = qz_r.dequantize(pkt.r_idx);
            let theta_rec: Array1<f64> = pkt
                .theta_indices
                .iter()
                .map(|&idx| qz_theta.dequantize(idx))
                .collect();

            let v_polar = polar_inverse(r_rec, &theta_rec);
            let v_chunk = rht.inverse(&v_polar);

            // QJL correction
            let correction = self.qjl.decode(&pkt.qjl_bits, pkt.chunk_size);
            let v_corrected = v_chunk + correction;

            let start = i * CHUNK_SIZE;
            let end = std::cmp::min(start + pkt.chunk_size, orig_dim);
            for (j, k) in (start..end).enumerate() {
                out[k] = v_corrected[j];
            }
        }

        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_gtm_round_trip() {
        let mut enc = GTMEncoder::new(4, 32, 42);
        let v: Array1<f64> = Array1::from_vec((0..64).map(|i| (i as f64 - 32.0) * 0.1).collect());

        let packets = enc.encode(&v);
        let mut dec = GTMDecoder::new(32);
        let v_rec = dec.decode(&packets, 64);

        let err = (&v - &v_rec).mapv(|x| x * x).sum().sqrt();
        assert!(err < v.mapv(|x| x * x).sum().sqrt() * 0.8);
    }

    #[test]
    fn test_packet_serialization() {
        let mut enc = GTMEncoder::new(4, 32, 42);
        let v: Array1<f64> = Array1::from_vec((0..64).map(|i| i as f64 * 0.05).collect());
        let packets = enc.encode(&v);

        for pkt in &packets {
            let json = serde_json::to_string(pkt).unwrap();
            let pkt2: GTMPacket = serde_json::from_str(&json).unwrap();
            assert_eq!(pkt2.chunk_idx, pkt.chunk_idx);
            assert_eq!(pkt2.r_idx, pkt.r_idx);
            assert_eq!(pkt2.theta_indices, pkt.theta_indices);
        }
    }
}
