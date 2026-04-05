//! Proactive Latent Consistency Check (LCC).
//!
//! Detects when quantization + prediction errors have become significant
//! enough to trigger a keyframe refresh, preventing semantic drift.

use ndarray::Array1;

/// Latent Consistency Check methods.
#[derive(Clone, Copy, Debug)]
pub enum LCCMethod {
    L2,
    Cosine,
    Norm,
}

/// Monitors quantization error in the latent residual and triggers
/// keyframe promotion when semantic drift becomes significant.
pub struct LCC {
    threshold: f64,
    method: LCCMethod,
    history_len: usize,
    error_history: Vec<f64>,
    pub trigger_count: usize,
}

impl LCC {
    pub fn new(threshold: f64, method: LCCMethod, history_len: usize) -> Self {
        Self {
            threshold,
            method,
            history_len,
            error_history: Vec::with_capacity(history_len),
            trigger_count: 0,
        }
    }

    /// Check whether quantization error warrants a keyframe.
    pub fn check(
        &mut self,
        delta_z: &Array1<f64>,
        delta_z_tilde: &Array1<f64>,
        z_ref: Option<&Array1<f64>>,
    ) -> bool {
        let err = self.compute_error(delta_z, delta_z_tilde, z_ref);
        self.error_history.push(err);
        if self.error_history.len() > self.history_len {
            self.error_history.remove(0);
        }

        let triggered = err > self.threshold;
        if triggered {
            self.trigger_count += 1;
        }
        triggered
    }

    /// Check if prediction ẑ_t is very far from z_t (cosine divergence).
    pub fn check_prediction_divergence(
        &mut self,
        z_t: &Array1<f64>,
        z_hat_t: &Array1<f64>,
    ) -> bool {
        let dot = z_t.dot(z_hat_t);
        let norm_a = z_t.mapv(|x| x * x).sum().sqrt();
        let norm_b = z_hat_t.mapv(|x| x * x).sum().sqrt();

        if norm_a < 1e-8 || norm_b < 1e-8 {
            return false;
        }

        let cos_sim = dot / (norm_a * norm_b);
        let diverged = cos_sim < (1.0 - self.threshold * 2.0);
        if diverged {
            self.trigger_count += 1;
        }
        diverged
    }

    fn compute_error(
        &self,
        delta_z: &Array1<f64>,
        delta_z_tilde: &Array1<f64>,
        z_ref: Option<&Array1<f64>>,
    ) -> f64 {
        match self.method {
            LCCMethod::L2 => (delta_z - delta_z_tilde).mapv(|x| x * x).sum().sqrt(),
            LCCMethod::Cosine => {
                let norm_a = delta_z.mapv(|x| x * x).sum().sqrt();
                let norm_b = delta_z_tilde.mapv(|x| x * x).sum().sqrt();
                if norm_a < 1e-8 || norm_b < 1e-8 {
                    return 0.0;
                }
                1.0 - delta_z.dot(delta_z_tilde) / (norm_a * norm_b)
            }
            LCCMethod::Norm => {
                let err = (delta_z - delta_z_tilde).mapv(|x| x * x).sum().sqrt();
                if let Some(z) = z_ref {
                    let ref_norm = z.mapv(|x| x * x).sum().sqrt().max(1e-8);
                    err / ref_norm
                } else {
                    err
                }
            }
        }
    }

    pub fn running_mean_error(&self) -> f64 {
        if self.error_history.is_empty() {
            return 0.0;
        }
        self.error_history.iter().sum::<f64>() / self.error_history.len() as f64
    }

    pub fn reset(&mut self) {
        self.error_history.clear();
        self.trigger_count = 0;
    }

    pub fn set_threshold(&mut self, threshold: f64) {
        self.threshold = threshold;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_lcc_no_trigger_small_error() {
        let mut lcc = LCC::new(0.15, LCCMethod::L2, 5);
        let delta_z: Array1<f64> = Array1::from_vec(vec![0.1, -0.05, 0.02]);
        let delta_z_tilde: Array1<f64> = Array1::from_vec(vec![0.11, -0.04, 0.025]);

        let triggered = lcc.check(&delta_z, &delta_z_tilde, None);
        assert!(!triggered);
    }

    #[test]
    fn test_lcc_trigger_large_error() {
        let mut lcc = LCC::new(0.15, LCCMethod::L2, 5);
        let delta_z: Array1<f64> = Array1::from_vec(vec![1.0, -0.8, 0.6]);
        let delta_z_tilde: Array1<f64> = Array1::from_vec(vec![-0.5, 0.4, -0.3]);

        let triggered = lcc.check(&delta_z, &delta_z_tilde, None);
        assert!(triggered);
    }

    #[test]
    fn test_prediction_divergence() {
        let mut lcc = LCC::new(0.15, LCCMethod::L2, 5);
        let z_t: Array1<f64> = Array1::from_vec(vec![1.0, 0.0, 0.0]);
        let z_hat: Array1<f64> = Array1::from_vec(vec![-1.0, 0.0, 0.0]);

        let diverged = lcc.check_prediction_divergence(&z_t, &z_hat);
        assert!(diverged);
    }
}
