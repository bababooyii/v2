//! MR-GWD Stage 1: Latent Synthesis
//!
//! Projects z_t (semantic latent) into pixel space at a base resolution (e.g., 256p)
//! using a learned MLP decoder. In production, this wraps a frozen SD-VAE decoder.

use image::{DynamicImage, RgbImage};
use ndarray::Array1;

/// Latent projector + decoder: z_t → base resolution image.
pub struct LatentSynth {
    latent_dim: usize,
    base_width: u32,
    base_height: u32,
    // Simplified MLP decoder weights
    fc1_w: ndarray::Array2<f64>,
    fc1_b: Array1<f64>,
    fc2_w: ndarray::Array2<f64>,
    fc2_b: Array1<f64>,
    // Output: 3 * base_width * base_height
    output_dim: usize,
}

impl LatentSynth {
    pub fn new(latent_dim: usize, base_width: u32, base_height: u32, seed: u64) -> Self {
        use rand::rngs::StdRng;
        use rand::SeedableRng;
        use rand_distr::{Distribution, Normal};

        let mut rng = StdRng::seed_from_u64(seed);
        let normal = Normal::new(0.0, 0.01).unwrap();

        let hidden = latent_dim * 4;
        let output_dim = (3 * base_width * base_height) as usize;

        let fc1_w =
            ndarray::Array2::from_shape_fn((hidden, latent_dim), |_| normal.sample(&mut rng));
        let fc1_b = Array1::zeros(hidden);
        let fc2_w =
            ndarray::Array2::from_shape_fn((output_dim, hidden), |_| normal.sample(&mut rng));
        let fc2_b = Array1::zeros(output_dim);

        Self {
            latent_dim,
            base_width,
            base_height,
            fc1_w,
            fc1_b,
            fc2_w,
            fc2_b,
            output_dim,
        }
    }

    /// Decode z_t → pixel array at base resolution.
    /// Returns flat array of RGB values in [-1, 1].
    pub fn decode(&self, z_t: &Array1<f64>) -> Array1<f64> {
        // Layer 1: GELU
        let h1 = self.fc1_w.dot(z_t) + &self.fc1_b;
        let h1 = h1.mapv(|x| {
            let c = 0.7978845608f64; // sqrt(2/PI)
            x * 0.5 * (1.0 + (x * c * (1.0 + 0.044715 * x * x)).tanh())
        });

        // Layer 2: tanh output
        let out = self.fc2_w.dot(&h1) + &self.fc2_b;
        out.mapv(|x| x.tanh())
    }

    /// Decode z_t → DynamicImage at base resolution.
    pub fn decode_image(&self, z_t: &Array1<f64>) -> DynamicImage {
        let pixels = self.decode(z_t);
        let mut img = RgbImage::new(self.base_width, self.base_height);

        for y in 0..self.base_height {
            for x in 0..self.base_width {
                let idx = ((y * self.base_width + x) * 3) as usize;
                let r = ((pixels[idx] + 1.0) / 2.0 * 255.0).clamp(0.0, 255.0) as u8;
                let g = ((pixels[idx + 1] + 1.0) / 2.0 * 255.0).clamp(0.0, 255.0) as u8;
                let b = ((pixels[idx + 2] + 1.0) / 2.0 * 255.0).clamp(0.0, 255.0) as u8;
                img.put_pixel(x, y, image::Rgb([r, g, b]));
            }
        }

        DynamicImage::ImageRgb8(img)
    }

    pub fn base_width(&self) -> u32 {
        self.base_width
    }

    pub fn base_height(&self) -> u32 {
        self.base_height
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::GenericImageView;

    #[test]
    fn test_decode_output_shape() {
        let synth = LatentSynth::new(512, 64, 64, 42);
        let z = Array1::from_vec((0..512).map(|i| (i as f64 / 512.0 - 0.5) * 2.0).collect());
        let pixels = synth.decode(&z);
        assert_eq!(pixels.len(), 3 * 64 * 64);
    }

    #[test]
    fn test_decode_image() {
        let synth = LatentSynth::new(256, 32, 32, 42);
        let z = Array1::from_vec((0..256).map(|i| (i as f64 / 256.0 - 0.5) * 2.0).collect());
        let img = synth.decode_image(&z);
        assert_eq!(img.dimensions(), (32, 32));
    }
}
