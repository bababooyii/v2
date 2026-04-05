//! ULEP Model — Unified Latent Encoder-Predictor
//!
//! Composes backbone feature extraction + encode head + predictor head
//! into a single streaming model with built-in state management.

use image::{DynamicImage, GenericImageView};
use ndarray::Array1;

use super::predictor::PredictorHead;

/// Encode head: projects backbone features into compact latent z_t.
pub struct EncodeHead {
    feat_dim: usize,
    latent_dim: usize,
    weights: ndarray::Array2<f64>,
    bias: Array1<f64>,
}

impl EncodeHead {
    pub fn new(feat_dim: usize, latent_dim: usize, seed: u64) -> Self {
        use rand::rngs::StdRng;
        use rand::SeedableRng;
        use rand_distr::{Distribution, Normal};

        let mut rng = StdRng::seed_from_u64(seed);
        let normal = Normal::new(0.0, (2.0 / feat_dim as f64).sqrt()).unwrap();

        // Input is feat_dim (from avg pool + attention pool = feat_dim * 2 total,
        // but extract_features returns feat_dim, and we double it via concat)
        let input_dim = feat_dim;
        let weights =
            ndarray::Array2::from_shape_fn((latent_dim, input_dim), |_| normal.sample(&mut rng));
        let bias = Array1::zeros(latent_dim);

        Self {
            feat_dim,
            latent_dim,
            weights,
            bias,
        }
    }

    /// Project features (N * feat_dim) → latent (latent_dim) with L2 norm.
    pub fn forward(&self, features: &Array1<f64>) -> Array1<f64> {
        // features already pooled to (feat_dim * 2) via avg + attention pool
        let mut z = self.weights.dot(features) + &self.bias;

        // L2-normalize
        let norm = z.mapv(|x| x * x).sum().sqrt();
        if norm > 1e-8 {
            z.mapv_inplace(|x| x / norm);
        }
        z
    }

    pub fn latent_dim(&self) -> usize {
        self.latent_dim
    }
}

/// Extracts features from an image using a simplified backbone.
/// In production, this wraps a pre-trained DINOv2 model via ONNX.
/// Here we use a deterministic hash-based feature extractor for the Rust engine.
pub fn extract_features(img: &DynamicImage, feat_dim: usize) -> Array1<f64> {
    let (w, h) = img.dimensions();
    let rgb = img.to_rgb8();

    // Multi-scale grid pooling: divide image into grid cells
    let grid_size = ((feat_dim as f64 / 3.0).sqrt().floor() as u32).max(2);
    let cell_w = w / grid_size;
    let cell_h = h / grid_size;

    let mut features = Vec::with_capacity(feat_dim);

    for gy in 0..grid_size {
        for gx in 0..grid_size {
            let mut r_sum = 0.0f64;
            let mut g_sum = 0.0f64;
            let mut b_sum = 0.0f64;
            let mut count = 0u64;

            for y in (gy * cell_h)..((gy + 1) * cell_h).min(h) {
                for x in (gx * cell_w)..((gx + 1) * cell_w).min(w) {
                    let pixel = rgb.get_pixel(x, y);
                    r_sum += pixel[0] as f64 / 255.0;
                    g_sum += pixel[1] as f64 / 255.0;
                    b_sum += pixel[2] as f64 / 255.0;
                    count += 1;
                }
            }

            let count = count.max(1) as f64;
            features.push(r_sum / count);
            features.push(g_sum / count);
            features.push(b_sum / count);
        }
    }

    // Pad or truncate to feat_dim
    while features.len() < feat_dim {
        features.push(0.0);
    }
    features.truncate(feat_dim);

    Array1::from_vec(features)
}

/// Unified Latent Encoder-Predictor.
pub struct ULEP {
    pub encode_head: EncodeHead,
    pub predictor: PredictorHead,
    feat_dim: usize,
    latent_dim: usize,

    // Streaming state
    z_t_minus_1: Option<Array1<f64>>,
    z_t_minus_2: Option<Array1<f64>>,
}

impl ULEP {
    pub fn new(latent_dim: usize, feat_dim: usize, seed: u64) -> Self {
        let hidden_dim = latent_dim * 2;
        Self {
            encode_head: EncodeHead::new(feat_dim, latent_dim, seed),
            predictor: PredictorHead::new(latent_dim, hidden_dim, seed.wrapping_add(1)),
            feat_dim,
            latent_dim,
            z_t_minus_1: None,
            z_t_minus_2: None,
        }
    }

    /// Encode a single frame to latent z_t.
    pub fn encode(&self, img: &DynamicImage) -> Array1<f64> {
        let features = extract_features(img, self.feat_dim);
        self.encode_head.forward(&features)
    }

    /// Predict ẑ_t from previous latents. Returns None if not enough history.
    pub fn predict(&self) -> Option<Array1<f64>> {
        match (&self.z_t_minus_1, &self.z_t_minus_2) {
            (Some(z1), Some(z2)) => Some(self.predictor.predict(z1, z2)),
            (Some(z1), None) => Some(z1.clone()), // repeat last
            _ => None,
        }
    }

    /// Update streaming state after encoding a frame.
    pub fn update_state(&mut self, z_t: Array1<f64>) {
        self.z_t_minus_2 = self.z_t_minus_1.take();
        self.z_t_minus_1 = Some(z_t);
    }

    /// Reset streaming state.
    pub fn reset_state(&mut self) {
        self.z_t_minus_1 = None;
        self.z_t_minus_2 = None;
    }

    pub fn has_enough_history(&self) -> bool {
        self.z_t_minus_1.is_some() && self.z_t_minus_2.is_some()
    }

    pub fn latent_dim(&self) -> usize {
        self.latent_dim
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::RgbImage;

    fn make_test_image(w: u32, h: u32) -> DynamicImage {
        let mut img = RgbImage::new(w, h);
        for y in 0..h {
            for x in 0..w {
                img.put_pixel(
                    x,
                    y,
                    image::Rgb([
                        ((x as f32 / w as f32) * 255.0) as u8,
                        ((y as f32 / h as f32) * 255.0) as u8,
                        128,
                    ]),
                );
            }
        }
        DynamicImage::ImageRgb8(img)
    }

    #[test]
    fn test_encode_output_shape() {
        let ulep = ULEP::new(512, 384, 42);
        let img = make_test_image(224, 224);
        let z = ulep.encode(&img);
        assert_eq!(z.len(), 512);
    }

    #[test]
    fn test_predict_requires_history() {
        let ulep = ULEP::new(256, 128, 42);
        assert!(ulep.predict().is_none());
    }

    #[test]
    fn test_state_management() {
        let mut ulep = ULEP::new(128, 64, 42);
        let img = make_test_image(64, 64);

        let z1 = ulep.encode(&img);
        ulep.update_state(z1.clone());
        assert!(ulep.has_enough_history() == false);

        let z2 = ulep.encode(&img);
        ulep.update_state(z2);
        assert!(ulep.has_enough_history() == true);

        let pred = ulep.predict();
        assert!(pred.is_some());
        assert_eq!(pred.unwrap().len(), 128);
    }
}
