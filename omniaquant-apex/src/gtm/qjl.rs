//! QJL (Quantized Johnson-Lindenstrauss) Bias Correction for GTM.
//!
//! After Lloyd-Max quantization, a systematic bias remains in the residual.
//! QJL projects the residual into a lower-dimensional sketch via a random
//! Johnson-Lindenstrauss matrix, 1-bit-quantizes the signs of the projection,
//! and transmits these sign bits as side information for bias correction.

use ndarray::{Array1, Array2};
use rand::rngs::StdRng;
use rand::Rng;
use rand::SeedableRng;

/// 1-bit Johnson-Lindenstrauss sketch-based bias correction.
#[derive(Clone)]
pub struct QJL {
    proj_dim: usize,
    seed: u64,
    proj_matrix: Array2<f64>,
}

impl QJL {
    pub fn new(proj_dim: usize, seed: u64) -> Self {
        // Build projection matrix lazily — we need orig_dim at encode time
        // So we store params and build on first encode
        Self {
            proj_dim,
            seed,
            proj_matrix: Array2::zeros((0, 0)),
        }
    }

    /// Build or rebuild the random JL matrix for a given input dimension.
    fn build_projection(&mut self, orig_dim: usize) {
        if self.proj_matrix.ncols() == orig_dim {
            return;
        }

        let mut rng = StdRng::seed_from_u64(self.seed);
        let scale = 1.0 / (self.proj_dim as f64).sqrt();

        let mut mat = Array2::zeros((self.proj_dim, orig_dim));
        for i in 0..self.proj_dim {
            for j in 0..orig_dim {
                mat[[i, j]] = if rng.gen_bool(0.5) { scale } else { -scale };
            }
        }
        self.proj_matrix = mat;
    }

    /// Compute 1-bit sketch of the residual vector.
    ///
    /// Returns a boolean vector of sign bits (length = proj_dim).
    pub fn encode(&mut self, residual: &Array1<f64>) -> Vec<bool> {
        let orig_dim = residual.len();
        self.build_projection(orig_dim);

        // projected = residual @ P^T  → (proj_dim,)
        let projected = self.proj_matrix.dot(residual);

        projected.iter().map(|&v| v > 0.0).collect()
    }

    /// Reconstruct approximate residual from 1-bit signs.
    ///
    /// sign_bits: bool vector of length proj_dim
    /// orig_dim: original residual dimension
    pub fn decode(&mut self, sign_bits: &[bool], orig_dim: usize) -> Array1<f64> {
        self.build_projection(orig_dim);

        // Map bool → ±1
        let s: Array1<f64> = sign_bits
            .iter()
            .map(|&b| if b { 1.0 } else { -1.0 })
            .collect();

        // correction = s @ P / proj_dim  → (orig_dim,)
        let p_t = self.proj_matrix.t();
        let correction = p_t.dot(&s);

        correction.mapv(|x| x / self.proj_dim as f64)
    }

    pub fn proj_dim(&self) -> usize {
        self.proj_dim
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_decode_shapes() {
        let mut qjl = QJL::new(64, 42);
        let residual: Array1<f64> =
            Array1::from_vec((0..128).map(|i| i as f64 * 0.1 - 6.4).collect());

        let bits = qjl.encode(&residual);
        assert_eq!(bits.len(), 64);

        let correction = qjl.decode(&bits, 128);
        assert_eq!(correction.len(), 128);
    }

    #[test]
    fn test_bias_reduction() {
        let mut qjl = QJL::new(128, 42);

        // Simulate systematic positive bias
        let residuals: Vec<Array1<f64>> = (0..50)
            .map(|_| {
                let base: Array1<f64> =
                    Array1::from_vec((0..256).map(|i| (i as f64 / 100.0 - 1.28) * 0.3).collect());
                base.mapv(|v| v + 0.5) // add bias
            })
            .collect();

        let mut total_before = 0.0f64;
        let mut total_after = 0.0f64;

        for biased in &residuals {
            let bits = qjl.encode(biased);
            let correction = qjl.decode(&bits, 256);
            let corrected = biased - &correction;

            total_before += biased.mean().unwrap().abs();
            total_after += corrected.mean().unwrap().abs();
        }

        let mean_before = total_before / residuals.len() as f64;
        let mean_after = total_after / residuals.len() as f64;

        assert!(
            mean_after < mean_before,
            "QJL should reduce bias: before={:.4}, after={:.4}",
            mean_before,
            mean_after
        );
    }
}
