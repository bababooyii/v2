//! ULEP Predictor Head — GRU-based temporal predictor.
//!
//! Given z_{t-1} and z_{t-2}, predicts ẑ_t using a gated recurrent architecture
//! with dual-input fusion.

use ndarray::Array1;

/// Dual-input GRU-based temporal predictor.
pub struct PredictorHead {
    latent_dim: usize,
    hidden_dim: usize,
    gate_w1: Array1<f64>,
    gate_w2: Array1<f64>,
    gate_bias: Array1<f64>,
    input_w: ndarray::Array2<f64>,
    input_b: Array1<f64>,
    output_w: ndarray::Array2<f64>,
    output_b: Array1<f64>,
}

impl PredictorHead {
    pub fn new(latent_dim: usize, hidden_dim: usize, seed: u64) -> Self {
        use rand::rngs::StdRng;
        use rand::SeedableRng;
        use rand_distr::{Distribution, Normal};

        let mut rng = StdRng::seed_from_u64(seed);
        let normal = Normal::new(0.0, 0.02).unwrap();

        let init_vec = |n: usize, rng: &mut StdRng| -> Array1<f64> {
            Array1::from_vec((0..n).map(|_| normal.sample(rng)).collect())
        };
        let init_mat = |r: usize, c: usize, rng: &mut StdRng| -> ndarray::Array2<f64> {
            ndarray::Array2::from_shape_fn((r, c), |_| normal.sample(rng))
        };

        Self {
            latent_dim,
            hidden_dim,
            gate_w1: init_vec(latent_dim, &mut rng),
            gate_w2: init_vec(latent_dim, &mut rng),
            gate_bias: Array1::zeros(latent_dim),
            input_w: init_mat(hidden_dim, latent_dim, &mut rng),
            input_b: init_vec(hidden_dim, &mut rng),
            output_w: init_mat(latent_dim, hidden_dim, &mut rng),
            output_b: init_vec(latent_dim, &mut rng),
        }
    }

    fn fuse(&self, z1: &Array1<f64>, z2: &Array1<f64>) -> Array1<f64> {
        let gate: Array1<f64> = z1
            .iter()
            .zip(z2.iter())
            .zip(
                self.gate_w1
                    .iter()
                    .zip(self.gate_w2.iter())
                    .zip(self.gate_bias.iter()),
            )
            .map(|((&z1i, &z2i), ((&w1i, &w2i), &bi))| {
                let logit = w1i * z1i + w2i * z2i + bi;
                1.0 / (1.0 + (-logit).exp())
            })
            .collect();

        gate.iter()
            .zip(z1.iter())
            .zip(z2.iter())
            .map(|((&g, &z1i), &z2i)| g * z1i + (1.0 - g) * z2i)
            .collect()
    }

    pub fn predict(&self, z_prev: &Array1<f64>, z_prev2: &Array1<f64>) -> Array1<f64> {
        assert_eq!(z_prev.len(), self.latent_dim);
        assert_eq!(z_prev2.len(), self.latent_dim);

        let fused = self.fuse(z_prev, z_prev2);

        let hidden = self.input_w.dot(&fused) + &self.input_b;
        let hidden = hidden.mapv(|x| {
            let c = 0.7978845608f64;
            x * 0.5 * (1.0 + (x * c * (1.0 + 0.044715 * x * x)).tanh())
        });

        let mut z_pred = self.output_w.dot(&hidden) + &self.output_b;

        let norm = z_pred.mapv(|x| x * x).sum().sqrt();
        if norm > 1e-8 {
            z_pred.mapv_inplace(|x| x / norm);
        }

        z_pred
    }

    pub fn predict_with_extrapolation(
        &self,
        z_prev: &Array1<f64>,
        z_prev2: &Array1<f64>,
        scale: f64,
    ) -> Array1<f64> {
        let z_pred_base = self.predict(z_prev, z_prev2);
        let velocity = z_prev - z_prev2;
        let mut z_pred = z_pred_base + velocity.mapv(|x| x * scale);

        let norm = z_pred.mapv(|x| x * x).sum().sqrt();
        if norm > 1e-8 {
            z_pred.mapv_inplace(|x| x / norm);
        }

        z_pred
    }

    pub fn latent_dim(&self) -> usize {
        self.latent_dim
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_predict_output_shape() {
        let pred = PredictorHead::new(512, 1024, 42);
        let z1 = Array1::zeros(512);
        let z2 = Array1::zeros(512);
        let z_hat = pred.predict(&z1, &z2);
        assert_eq!(z_hat.len(), 512);
    }

    #[test]
    fn test_predict_unit_norm() {
        use rand::rngs::StdRng;
        use rand::SeedableRng;

        let pred = PredictorHead::new(256, 512, 42);
        let mut rng = StdRng::seed_from_u64(0);
        let z1: Array1<f64> =
            Array1::from_vec((0..256).map(|_| rand::Rng::gen(&mut rng)).collect());
        let z2: Array1<f64> =
            Array1::from_vec((0..256).map(|_| rand::Rng::gen(&mut rng)).collect());

        let z_hat = pred.predict(&z1, &z2);
        let norm = z_hat.mapv(|x| x * x).sum().sqrt();
        assert!((norm - 1.0).abs() < 1e-6);
    }
}
