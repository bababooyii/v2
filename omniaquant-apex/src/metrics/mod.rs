//! Codec Evaluation Metrics.
//!
//! Provides PSNR, SSIM, bitrate, and bits-per-pixel calculations.

use ndarray::Array1;
use std::collections::HashMap;

/// Compute Peak Signal-to-Noise Ratio.
///
/// Inputs are arrays of pixel values in [-1, 1].
/// max_val = 2.0 for [-1, 1] range.
pub fn compute_psnr(original: &Array1<f64>, decoded: &Array1<f64>, max_val: f64) -> f64 {
    let n = original.len().min(decoded.len());
    let mut mse = 0.0f64;
    for i in 0..n {
        let diff = original[i] - decoded[i];
        mse += diff * diff;
    }
    mse /= n as f64;

    if mse < 1e-12 {
        return f64::INFINITY;
    }
    10.0 * (max_val.powi(2) / mse).log10()
}

/// Compute Structural Similarity Index (simplified, single-scale).
pub fn compute_ssim(original: &Array1<f64>, decoded: &Array1<f64>) -> f64 {
    let n = original.len().min(decoded.len());
    if n == 0 {
        return 0.0;
    }

    let mean_orig = original.iter().take(n).sum::<f64>() / n as f64;
    let mean_dec = decoded.iter().take(n).sum::<f64>() / n as f64;

    let var_orig = original
        .iter()
        .take(n)
        .map(|&x| (x - mean_orig).powi(2))
        .sum::<f64>()
        / n as f64;
    let var_dec = decoded
        .iter()
        .take(n)
        .map(|&x| (x - mean_dec).powi(2))
        .sum::<f64>()
        / n as f64;

    let cov: f64 = original
        .iter()
        .take(n)
        .zip(decoded.iter().take(n))
        .map(|(&a, &b)| (a - mean_orig) * (b - mean_dec))
        .sum::<f64>()
        / n as f64;

    let c1 = 0.01f64.powi(2);
    let c2 = 0.03f64.powi(2);

    let numerator = (2.0 * mean_orig * mean_dec + c1) * (2.0 * cov + c2);
    let denominator = (mean_orig.powi(2) + mean_dec.powi(2) + c1) * (var_orig + var_dec + c2);

    if denominator < 1e-12 {
        return 1.0;
    }
    numerator / denominator
}

/// Compute average bitrate in Mbps given per-frame packet sizes and fps.
pub fn compute_bitrate(packet_bytes: &[usize], fps: f64) -> f64 {
    if packet_bytes.is_empty() {
        return 0.0;
    }
    let total_bytes: usize = packet_bytes.iter().sum();
    let n_frames = packet_bytes.len();
    let duration_s = n_frames as f64 / fps;
    (total_bytes as f64 * 8.0) / duration_s / 1e6
}

/// Bits per pixel for a single frame.
pub fn compute_bpp(packet_bytes: usize, width: u32, height: u32) -> f64 {
    let n_pixels = width as usize * height as usize;
    if n_pixels == 0 {
        return 0.0;
    }
    (packet_bytes as f64 * 8.0) / n_pixels as f64
}

/// Rolling metrics accumulator for streaming evaluation.
pub struct MetricsAccumulator {
    fps: f64,
    psnr_values: Vec<f64>,
    ssim_values: Vec<f64>,
    packet_bytes: Vec<usize>,
    keyframe_indices: Vec<usize>,
    lcc_trigger_indices: Vec<usize>,
}

impl MetricsAccumulator {
    pub fn new(fps: f64) -> Self {
        Self {
            fps,
            psnr_values: Vec::new(),
            ssim_values: Vec::new(),
            packet_bytes: Vec::new(),
            keyframe_indices: Vec::new(),
            lcc_trigger_indices: Vec::new(),
        }
    }

    /// Update metrics with a new frame.
    pub fn update(
        &mut self,
        original: &Array1<f64>,
        decoded: &Array1<f64>,
        packet_bytes: usize,
        is_keyframe: bool,
        lcc_triggered: bool,
    ) {
        let frame_idx = self.packet_bytes.len();

        self.psnr_values.push(compute_psnr(original, decoded, 2.0));
        self.ssim_values.push(compute_ssim(original, decoded));
        self.packet_bytes.push(packet_bytes);

        if is_keyframe {
            self.keyframe_indices.push(frame_idx);
        }
        if lcc_triggered {
            self.lcc_trigger_indices.push(frame_idx);
        }
    }

    pub fn mean_psnr(&self) -> f64 {
        if self.psnr_values.is_empty() {
            return 0.0;
        }
        self.psnr_values.iter().sum::<f64>() / self.psnr_values.len() as f64
    }

    pub fn mean_ssim(&self) -> f64 {
        if self.ssim_values.is_empty() {
            return 0.0;
        }
        self.ssim_values.iter().sum::<f64>() / self.ssim_values.len() as f64
    }

    pub fn avg_bitrate_mbps(&self) -> f64 {
        compute_bitrate(&self.packet_bytes, self.fps)
    }

    /// Bitrate over last 30 frames.
    pub fn instantaneous_bitrate_mbps(&self) -> f64 {
        let recent = if self.packet_bytes.len() > 30 {
            &self.packet_bytes[self.packet_bytes.len() - 30..]
        } else {
            &self.packet_bytes
        };
        compute_bitrate(recent, self.fps)
    }

    /// Summary as a HashMap.
    pub fn summary(&self) -> HashMap<String, f64> {
        let mut map = HashMap::new();
        map.insert("frames".to_string(), self.packet_bytes.len() as f64);
        map.insert("avg_psnr_db".to_string(), self.mean_psnr());
        map.insert("avg_ssim".to_string(), self.mean_ssim());
        map.insert("avg_bitrate_mbps".to_string(), self.avg_bitrate_mbps());
        map.insert("keyframes".to_string(), self.keyframe_indices.len() as f64);
        map.insert(
            "lcc_triggers".to_string(),
            self.lcc_trigger_indices.len() as f64,
        );
        let total_bytes: usize = self.packet_bytes.iter().sum();
        map.insert("total_bytes".to_string(), total_bytes as f64);
        map
    }

    pub fn reset(&mut self) {
        self.psnr_values.clear();
        self.ssim_values.clear();
        self.packet_bytes.clear();
        self.keyframe_indices.clear();
        self.lcc_trigger_indices.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_psnr_identical() {
        let a = Array1::from_vec(vec![0.5, -0.3, 0.1]);
        let b = a.clone();
        let psnr = compute_psnr(&a, &b, 2.0);
        assert!(psnr.is_infinite());
    }

    #[test]
    fn test_psnr_different() {
        let a = Array1::from_vec(vec![1.0, 1.0, 1.0]);
        let b = Array1::from_vec(vec![0.0, 0.0, 0.0]);
        let psnr = compute_psnr(&a, &b, 2.0);
        assert!(psnr > 0.0 && psnr < 50.0);
    }

    #[test]
    fn test_ssim_identical() {
        let a = Array1::from_vec(vec![0.5, 0.3, 0.1, 0.7, 0.2]);
        let ssim = compute_ssim(&a, &a);
        assert!((ssim - 1.0).abs() < 1e-6);
    }

    #[test]
    fn test_metrics_accumulator() {
        let mut metrics = MetricsAccumulator::new(30.0);
        let a = Array1::from_vec(vec![0.5, -0.3, 0.1]);
        let b = Array1::from_vec(vec![0.48, -0.28, 0.12]);

        metrics.update(&a, &b, 1000, true, false);
        metrics.update(&a, &b, 500, false, false);

        assert_eq!(metrics.packet_bytes.len(), 2);
        assert!(metrics.mean_psnr() > 0.0);
        assert!(metrics.mean_ssim() > 0.0);
    }
}
