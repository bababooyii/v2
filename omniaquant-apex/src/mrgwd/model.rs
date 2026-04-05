//! MR-GWD Model — Multi-Resolution Generative World Decoder
//!
//! Composes LatentSynth + temporal upsampling into a unified model.
//! Manages the previous-frame buffer for temporal consistency.

use image::DynamicImage;
use ndarray::Array1;

use super::synth::LatentSynth;

/// Multi-Resolution Generative World Decoder.
pub struct MRGWD {
    latent_synth: LatentSynth,
    target_width: u32,
    target_height: u32,
    prev_frame: Option<DynamicImage>,
}

impl MRGWD {
    pub fn new(
        latent_dim: usize,
        base_width: u32,
        base_height: u32,
        target_width: u32,
        target_height: u32,
        seed: u64,
    ) -> Self {
        Self {
            latent_synth: LatentSynth::new(latent_dim, base_width, base_height, seed),
            target_width,
            target_height,
            prev_frame: None,
        }
    }

    /// Synthesize a frame from z_t.
    ///
    /// z_t: (latent_dim,) latent state
    /// returns: DynamicImage at target resolution
    pub fn synthesize(&mut self, z_t: &Array1<f64>) -> DynamicImage {
        // Stage 1: base resolution image
        let base_img = self.latent_synth.decode_image(z_t);

        // Stage 2: upscale to target with temporal blending
        let upscaled = self.upscale_with_temporal(&base_img);

        self.prev_frame = Some(upscaled.clone());
        upscaled
    }

    /// Upscale base image to target resolution with temporal consistency.
    fn upscale_with_temporal(&self, base: &DynamicImage) -> DynamicImage {
        // Bilinear upscale
        let upscaled = self.bilinear_resize(base, self.target_width, self.target_height);

        // Blend with warped previous frame for temporal consistency
        if let Some(ref prev) = self.prev_frame {
            let prev_resized = self.bilinear_resize(prev, self.target_width, self.target_height);
            self.temporal_blend(&upscaled, &prev_resized, 0.85)
        } else {
            upscaled
        }
    }

    /// Bilinear resize using image crate.
    fn bilinear_resize(&self, img: &DynamicImage, w: u32, h: u32) -> DynamicImage {
        img.resize_exact(w, h, image::imageops::FilterType::Triangle)
    }

    /// Temporal blend: 85% current + 15% previous for flicker reduction.
    fn temporal_blend(&self, curr: &DynamicImage, prev: &DynamicImage, alpha: f64) -> DynamicImage {
        let curr_rgb = curr.to_rgb8();
        let prev_rgb = prev.to_rgb8();
        let w = curr_rgb.width();
        let h = curr_rgb.height();

        let mut out = RgbImage::new(w, h);
        for y in 0..h {
            for x in 0..w {
                let c = curr_rgb.get_pixel(x, y);
                let p = prev_rgb.get_pixel(x, y);
                out.put_pixel(
                    x,
                    y,
                    image::Rgb([
                        ((alpha * c[0] as f64 + (1.0 - alpha) * p[0] as f64) as u8),
                        ((alpha * c[1] as f64 + (1.0 - alpha) * p[1] as f64) as u8),
                        ((alpha * c[2] as f64 + (1.0 - alpha) * p[2] as f64) as u8),
                    ]),
                );
            }
        }
        DynamicImage::ImageRgb8(out)
    }

    /// Reset state (call at start of new video sequence).
    pub fn reset_state(&mut self) {
        self.prev_frame = None;
    }

    pub fn target_width(&self) -> u32 {
        self.target_width
    }

    pub fn target_height(&self) -> u32 {
        self.target_height
    }
}

use image::RgbImage;

#[cfg(test)]
mod tests {
    use super::*;
    use image::GenericImageView;

    #[test]
    fn test_synthesize_output_size() {
        let mut mrgwd = MRGWD::new(512, 64, 64, 256, 256, 42);
        let z = Array1::from_vec((0..512).map(|i| (i as f64 / 512.0 - 0.5) * 2.0).collect());
        let frame = mrgwd.synthesize(&z);
        assert_eq!(frame.dimensions(), (256, 256));
    }

    #[test]
    fn test_temporal_consistency() {
        let mut mrgwd = MRGWD::new(256, 32, 32, 128, 128, 42);
        let z1 = Array1::from_vec((0..256).map(|i| (i as f64 / 256.0) * 2.0 - 1.0).collect());
        let z2 = Array1::from_vec(
            (0..256)
                .map(|i| ((i + 1) as f64 / 256.0) * 2.0 - 1.0)
                .collect(),
        );

        let f1 = mrgwd.synthesize(&z1);
        let f2 = mrgwd.synthesize(&z2);

        // Both should be same size
        assert_eq!(f1.dimensions(), (128, 128));
        assert_eq!(f2.dimensions(), (128, 128));
    }
}
