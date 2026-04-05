//! Randomized Hadamard Transform (RHT)
//!
//! Applies a Rademacher sign flip then a Fast Walsh-Hadamard Transform
//! to spread energy uniformly before polar quantization.

use ndarray::Array1;
use rand::rngs::StdRng;
use rand::Rng;
use rand::SeedableRng;

fn next_power_of_two(n: usize) -> usize {
    if n == 0 {
        return 1;
    }
    let mut p = 1;
    while p < n {
        p <<= 1;
    }
    p
}

/// Fast Walsh-Hadamard Transform (unnormalized, in-place).
fn fwht(x: &mut [f64]) {
    let n = x.len();
    assert!(n.is_power_of_two(), "Length must be a power of 2");
    let mut h = 1;
    while h < n {
        let mut i = 0;
        while i < n {
            for j in i..(i + h) {
                let a = x[j];
                let b = x[j + h];
                x[j] = a + b;
                x[j + h] = a - b;
            }
            i += 2 * h;
        }
        h <<= 1;
    }
}

/// Randomized Hadamard Transform.
///
/// v' = D @ H @ v where D is a random Rademacher diagonal.
/// Normalised so the transform is an isometry (up to sign).
#[derive(Clone)]
pub struct RHT {
    dim: usize,
    padded_dim: usize,
    signs: Vec<f64>,
}

impl RHT {
    pub fn new(dim: usize, seed: u64) -> Self {
        let padded_dim = next_power_of_two(dim);
        let mut rng = StdRng::seed_from_u64(seed);
        let signs: Vec<f64> = (0..padded_dim)
            .map(|_| if rng.gen_bool(0.5) { 1.0 } else { -1.0 })
            .collect();

        Self {
            dim,
            padded_dim,
            signs,
        }
    }

    /// Apply RHT. v: (dim) → (padded_dim).
    pub fn forward(&self, v: &Array1<f64>) -> Array1<f64> {
        let mut buf = vec![0.0f64; self.padded_dim];

        // Pad and apply sign flip
        for i in 0..v.len() {
            buf[i] = v[i] * self.signs[i];
        }

        // FWHT
        fwht(&mut buf);

        // Normalize
        let norm = (self.padded_dim as f64).sqrt();
        for x in &mut buf {
            *x /= norm;
        }

        Array1::from_vec(buf)
    }

    /// Invert RHT. v_hat: (padded_dim) → (dim).
    pub fn inverse(&self, v_hat: &Array1<f64>) -> Array1<f64> {
        let mut buf = v_hat.to_vec();

        // Un-normalize: H^{-1} = H / n
        let norm = (self.padded_dim as f64).sqrt();
        for x in &mut buf {
            *x *= norm;
        }

        fwht(&mut buf);

        let n = self.padded_dim as f64;
        for x in &mut buf {
            *x /= n;
        }

        // Apply sign flip and truncate
        let mut out = Vec::with_capacity(self.dim);
        for i in 0..self.dim {
            out.push(buf[i] * self.signs[i]);
        }

        Array1::from_vec(out)
    }

    pub fn dim(&self) -> usize {
        self.dim
    }

    pub fn padded_dim(&self) -> usize {
        self.padded_dim
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_abs_diff_eq;

    #[test]
    fn test_rht_round_trip() {
        let rht = RHT::new(32, 42);
        let v: Array1<f64> = Array1::range(0.0, 32.0, 1.0);
        let v_t = rht.forward(&v);
        let v_rec = rht.inverse(&v_t);

        for i in 0..v.len() {
            assert_abs_diff_eq!(v[i], v_rec[i], epsilon = 1e-10);
        }
    }

    #[test]
    fn test_rht_norm_preservation() {
        let rht = RHT::new(64, 7);
        let v: Array1<f64> = Array1::from_vec((0..64).map(|i| i as f64).collect());
        let v_t = rht.forward(&v);

        let norm_orig = v.dot(&v).sqrt();
        let norm_t = v_t.dot(&v_t).sqrt();

        assert!((norm_orig - norm_t).abs() < 0.05);
    }
}
