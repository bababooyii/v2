//! Sparse Latent Coding — transmits only top-k components of Δz_t.
//!
//! Instead of transmitting the full residual vector, OmniQuant-Apex
//! identifies the k most significant components by magnitude and transmits
//! only their indices + values.

use ndarray::Array1;

/// Top-k sparse encoder/decoder for latent residuals.
pub struct SparseCoder {
    top_k_fraction: f64,
    min_k: usize,
}

impl SparseCoder {
    pub fn new(top_k_fraction: f64, min_k: usize) -> Self {
        Self {
            top_k_fraction,
            min_k,
        }
    }

    pub fn get_k(&self, dim: usize) -> usize {
        self.min_k.max((dim as f64 * self.top_k_fraction) as usize)
    }

    /// Select top-k components of delta_z by absolute value.
    ///
    /// Returns (indices, values) where indices are positions in the original vector.
    pub fn encode_sparse(
        &self,
        delta_z: &Array1<f64>,
        k: Option<usize>,
    ) -> (Vec<u16>, Array1<f64>) {
        let dim = delta_z.len();
        let k = k.unwrap_or_else(|| self.get_k(dim)).min(dim);

        // Create (index, abs_value) pairs and sort by abs value descending
        let mut pairs: Vec<(usize, f64)> = delta_z
            .iter()
            .enumerate()
            .map(|(i, &v)| (i, v.abs()))
            .collect();
        pairs.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());

        let indices: Vec<u16> = pairs[..k].iter().map(|(i, _)| *i as u16).collect();
        let values: Array1<f64> = pairs[..k].iter().map(|(i, _)| delta_z[*i]).collect();

        (indices, values)
    }

    /// Reconstruct dense delta_z from sparse (indices, values) pair.
    pub fn decode_sparse(&self, indices: &[u16], values: &Array1<f64>, dim: usize) -> Array1<f64> {
        let mut delta_z = Array1::zeros(dim);
        for (i, &idx) in indices.iter().enumerate() {
            delta_z[idx as usize] = values[i];
        }
        delta_z
    }

    /// Compute fraction of energy (L2 squared) retained by top-k sparse code.
    pub fn energy_retention(&self, delta_z: &Array1<f64>, k: Option<usize>) -> f64 {
        let dim = delta_z.len();
        let k = k.unwrap_or_else(|| self.get_k(dim)).min(dim);

        let total_energy = delta_z.mapv(|x| x * x).sum();
        if total_energy < 1e-12 {
            return 1.0;
        }

        let mut sq: Vec<f64> = delta_z.iter().map(|&x| x * x).collect();
        sq.sort_by(|a, b| b.partial_cmp(a).unwrap());

        let topk_energy: f64 = sq[..k].iter().sum();
        topk_energy / total_energy
    }

    /// Estimate bits required to transmit sparse representation.
    pub fn estimate_bits(&self, dim: usize, k: Option<usize>, value_bits: usize) -> usize {
        let k = k.unwrap_or_else(|| self.get_k(dim));
        let index_bits = (dim as f64).log2().ceil() as usize;
        k * (index_bits + value_bits)
    }

    pub fn set_top_k_fraction(&mut self, fraction: f64) {
        self.top_k_fraction = fraction;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sparse_round_trip() {
        let coder = SparseCoder::new(0.25, 8);
        let mut delta_z = Array1::zeros(128);
        delta_z[10] = 5.0;
        delta_z[50] = -3.0;
        delta_z[100] = 2.0;

        let k = coder.get_k(128);
        let (indices, values) = coder.encode_sparse(&delta_z, Some(k));
        let reconstructed = coder.decode_sparse(&indices, &values, 128);

        // Top components should be recovered
        assert!((reconstructed[10] - 5.0).abs() < 1e-10);
    }

    #[test]
    fn test_energy_retention() {
        let coder = SparseCoder::new(0.5, 8);
        let mut delta_z = Array1::zeros(100);
        delta_z[0] = 10.0;
        delta_z[1] = 1.0;

        let retention = coder.energy_retention(&delta_z, Some(1));
        // Top 1 captures 100/(100+1) ≈ 99%
        assert!(retention > 0.98);
    }
}
