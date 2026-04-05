//! Adaptive Lloyd-Max Scalar Quantizer for GTM.
//!
//! Fits optimal codebooks for any bit-width via iterative Lloyd-Max algorithm,
//! then provides fast quantize/dequantize operations for latent residuals.

use ndarray::Array1;
use statrs::distribution::{ContinuousCDF, Normal};

/// 1-D Lloyd-Max quantizer with pre-computed codebooks.
///
/// Supports 1–8 bit quantization. Codebooks are fit on calibration data
/// and can be initialized with Gaussian-optimal boundaries.
#[derive(Clone)]
pub struct LloydMaxQuantizer {
    n_bits: usize,
    n_levels: usize,
    pub centroids: Array1<f64>,
    pub boundaries: Array1<f64>,
}

impl LloydMaxQuantizer {
    pub fn new(n_bits: usize) -> Self {
        assert!((1..=8).contains(&n_bits), "bit-width must be 1..8");
        let n_levels = 1 << n_bits;
        Self {
            n_bits,
            n_levels,
            centroids: Array1::zeros(n_levels),
            boundaries: Array1::zeros(n_levels + 1),
        }
    }

    /// Calibrate codebook on 1-D data.
    pub fn fit(&mut self, data: &Array1<f64>) {
        let n = data.len();
        // Sort data for percentile computation
        let mut sorted = data.to_vec();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());

        // Init centroids by uniform percentile spacing
        let mut centroids = vec![0.0f64; self.n_levels];
        for k in 0..self.n_levels {
            let p = (k as f64 + 0.5) / self.n_levels as f64;
            let idx = (p * n as f64).clamp(0.0, (n - 1) as f64) as usize;
            centroids[k] = sorted[idx];
        }

        // Iterative Lloyd-Max
        for _ in 0..100 {
            // Boundaries = midpoints between centroids
            let mut boundaries = vec![f64::NEG_INFINITY];
            for k in 0..(self.n_levels - 1) {
                boundaries.push((centroids[k] + centroids[k + 1]) / 2.0);
            }
            boundaries.push(f64::INFINITY);

            // Update centroids = conditional mean in each cell
            let mut new_centroids = vec![0.0f64; self.n_levels];
            let mut converged = true;

            for k in 0..self.n_levels {
                let mut sum = 0.0f64;
                let mut count = 0usize;
                for &x in data.iter() {
                    if x >= boundaries[k] && x < boundaries[k + 1] {
                        sum += x;
                        count += 1;
                    }
                }
                if count > 0 {
                    new_centroids[k] = sum / count as f64;
                } else {
                    new_centroids[k] = centroids[k];
                }
                if (new_centroids[k] - centroids[k]).abs() > 1e-8 {
                    converged = false;
                }
            }

            centroids = new_centroids;
            if converged {
                break;
            }
        }

        // Final boundaries
        let mut boundaries = vec![f64::NEG_INFINITY];
        for k in 0..(self.n_levels - 1) {
            boundaries.push((centroids[k] + centroids[k + 1]) / 2.0);
        }
        boundaries.push(f64::INFINITY);

        self.centroids = Array1::from_vec(centroids);
        self.boundaries = Array1::from_vec(boundaries);
    }

    /// Quick init: Gaussian-optimal boundaries without calibration data.
    pub fn default_fit(&mut self, scale: f64) {
        let normal = Normal::new(0.0, 1.0).unwrap();
        let mut centroids = Vec::with_capacity(self.n_levels);

        for k in 0..self.n_levels {
            let p = (k as f64 + 0.5) / self.n_levels as f64;
            centroids.push(normal.inverse_cdf(p) * scale);
        }

        let mut boundaries = vec![f64::NEG_INFINITY];
        for k in 0..(self.n_levels - 1) {
            boundaries.push((centroids[k] + centroids[k + 1]) / 2.0);
        }
        boundaries.push(f64::INFINITY);

        self.centroids = Array1::from_vec(centroids);
        self.boundaries = Array1::from_vec(boundaries);
    }

    /// Quantize scalar values to integer indices.
    pub fn quantize(&self, x: f64) -> u16 {
        let mut best_idx = 0u16;
        let mut best_dist = f64::INFINITY;

        for (k, &c) in self.centroids.iter().enumerate() {
            let dist = (x - c).abs();
            if dist < best_dist {
                best_dist = dist;
                best_idx = k as u16;
            }
        }

        best_idx
    }

    /// Quantize a vector of values to indices.
    pub fn quantize_vec(&self, v: &Array1<f64>) -> Array1<u16> {
        v.mapv(|x| self.quantize(x))
    }

    /// Map integer index back to centroid value.
    pub fn dequantize(&self, index: u16) -> f64 {
        self.centroids[index as usize]
    }

    /// Dequantize a vector of indices.
    pub fn dequantize_vec(&self, indices: &Array1<u16>) -> Array1<f64> {
        indices.mapv(|i| self.dequantize(i))
    }

    pub fn n_bits(&self) -> usize {
        self.n_bits
    }

    pub fn n_levels(&self) -> usize {
        self.n_levels
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_abs_diff_eq;

    #[test]
    fn test_default_fit_quantize() {
        let mut qz = LloydMaxQuantizer::new(4);
        qz.default_fit(1.0);

        let x = 0.5;
        let idx = qz.quantize(x);
        let x_hat = qz.dequantize(idx);

        assert!(idx < 16);
        assert!((x - x_hat).abs() < 0.3);
    }

    #[test]
    fn test_error_decreases_with_bits() {
        let x: Array1<f64> =
            Array1::from_vec((0..5000).map(|i| (i as f64 / 1000.0) - 2.5).collect());

        let mut qz2 = LloydMaxQuantizer::new(2);
        qz2.default_fit(1.0);
        let err2 = x
            .iter()
            .map(|&v| {
                let q = qz2.dequantize(qz2.quantize(v));
                (v - q).powi(2)
            })
            .sum::<f64>()
            / x.len() as f64;

        let mut qz4 = LloydMaxQuantizer::new(4);
        qz4.default_fit(1.0);
        let err4 = x
            .iter()
            .map(|&v| {
                let q = qz4.dequantize(qz4.quantize(v));
                (v - q).powi(2)
            })
            .sum::<f64>()
            / x.len() as f64;

        let mut qz8 = LloydMaxQuantizer::new(8);
        qz8.default_fit(1.0);
        let err8 = x
            .iter()
            .map(|&v| {
                let q = qz8.dequantize(qz8.quantize(v));
                (v - q).powi(2)
            })
            .sum::<f64>()
            / x.len() as f64;

        assert!(err2 > err4, "2-bit error should be > 4-bit");
        assert!(err4 > err8, "4-bit error should be > 8-bit");
    }

    #[test]
    fn test_round_trip_index_validity() {
        let mut qz = LloydMaxQuantizer::new(3);
        qz.default_fit(2.0);

        for &v in &[-2.0, -0.5, 0.0, 0.3, 1.7, 2.0] {
            let idx = qz.quantize(v);
            assert!(idx < 8);
            let rec = qz.dequantize(idx);
            assert!((v - rec).abs() < 2.0);
        }
    }
}
