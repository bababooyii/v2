//! Adaptive Bitrate Controller
//!
//! Dynamically adjusts encoding parameters (GTM bits, sparse fraction, keyframe interval)
//! based on network conditions, similar to DASH/HLS but at the latent level.

use serde::{Deserialize, Serialize};

/// Network quality estimate.
#[derive(Clone, Copy, Debug)]
pub struct NetworkEstimate {
    pub available_bandwidth_mbps: f64,
    pub packet_loss_rate: f64,
    pub rtt_ms: f64,
    pub jitter_ms: f64,
}

/// Encoding configuration produced by the ABR controller.
#[derive(Clone, Serialize, Deserialize, Debug)]
pub struct EncodeConfig {
    pub gtm_bits_keyframe: usize,
    pub gtm_bits_predictive: usize,
    pub sparse_fraction: f64,
    pub keyframe_interval: usize,
    pub lcc_threshold: f64,
    pub qjl_proj_dim: usize,
}

/// Adaptive Bitrate controller.
///
/// Adjusts encoding parameters to match available bandwidth while
/// maintaining perceptual quality targets.
pub struct AdaptiveBitrate {
    // Target constraints
    pub target_bitrate_mbps: f64,
    pub min_bitrate_mbps: f64,
    pub max_bitrate_mbps: f64,
    pub target_psnr_db: f64,

    // Current config
    config: EncodeConfig,

    // Smoothing
    recent_bitrates: Vec<f64>,
    smoothing_window: usize,

    // History
    frame_count: usize,
    total_bytes: usize,
}

impl AdaptiveBitrate {
    pub fn new(target_bitrate_mbps: f64) -> Self {
        Self {
            target_bitrate_mbps,
            min_bitrate_mbps: 0.1,
            max_bitrate_mbps: 50.0,
            target_psnr_db: 35.0,
            config: EncodeConfig {
                gtm_bits_keyframe: 6,
                gtm_bits_predictive: 3,
                sparse_fraction: 0.25,
                keyframe_interval: 60,
                lcc_threshold: 0.15,
                qjl_proj_dim: 64,
            },
            recent_bitrates: Vec::new(),
            smoothing_window: 30,
            frame_count: 0,
            total_bytes: 0,
        }
    }

    /// Update with per-frame packet size.
    pub fn update(&mut self, packet_bytes: usize, fps: f64) {
        self.frame_count += 1;
        self.total_bytes += packet_bytes;

        let instant_bitrate = (packet_bytes as f64 * 8.0 * fps) / 1e6;
        self.recent_bitrates.push(instant_bitrate);
        if self.recent_bitrates.len() > self.smoothing_window {
            self.recent_bitrates.remove(0);
        }
    }

    /// Get smoothed recent bitrate in Mbps.
    pub fn smoothed_bitrate(&self) -> f64 {
        if self.recent_bitrates.is_empty() {
            return 0.0;
        }
        self.recent_bitrates.iter().sum::<f64>() / self.recent_bitrates.len() as f64
    }

    /// Adjust encoding config based on network conditions.
    pub fn adjust(&mut self, network: &NetworkEstimate) -> &EncodeConfig {
        let available = network.available_bandwidth_mbps;
        let loss = network.packet_loss_rate;

        // Target bitrate: use 80% of available bandwidth (headroom for overhead)
        let target = (available * 0.8).clamp(self.min_bitrate_mbps, self.max_bitrate_mbps);

        if target < self.target_bitrate_mbps * 0.5 {
            // Severe congestion: aggressively reduce quality
            self.config.gtm_bits_keyframe = self.config.gtm_bits_keyframe.max(2).min(4);
            self.config.gtm_bits_predictive = self.config.gtm_bits_predictive.max(1).min(2);
            self.config.sparse_fraction = (self.config.sparse_fraction - 0.05).max(0.05);
            self.config.keyframe_interval = (self.config.keyframe_interval + 10).min(180);
            self.config.lcc_threshold = (self.config.lcc_threshold + 0.02).min(0.5);
        } else if target < self.target_bitrate_mbps {
            // Moderate congestion: slight reduction
            self.config.gtm_bits_keyframe = self.config.gtm_bits_keyframe.saturating_sub(1).max(3);
            self.config.gtm_bits_predictive =
                self.config.gtm_bits_predictive.saturating_sub(1).max(1);
            self.config.sparse_fraction = (self.config.sparse_fraction - 0.02).max(0.1);
        } else if target > self.target_bitrate_mbps * 1.5 {
            // Plenty of bandwidth: increase quality
            self.config.gtm_bits_keyframe = (self.config.gtm_bits_keyframe + 1).min(8);
            self.config.gtm_bits_predictive = (self.config.gtm_bits_predictive + 1).min(6);
            self.config.sparse_fraction = (self.config.sparse_fraction + 0.05).min(1.0);
            self.config.keyframe_interval = self.config.keyframe_interval.saturating_sub(5).max(15);
            self.config.lcc_threshold = (self.config.lcc_threshold - 0.01).max(0.05);
        }

        // High packet loss: increase keyframe frequency for faster recovery
        if loss > 0.05 {
            self.config.keyframe_interval = (self.config.keyframe_interval / 2).max(10);
        }

        self.target_bitrate_mbps = target;
        &self.config
    }

    /// Get current encoding config.
    pub fn config(&self) -> &EncodeConfig {
        &self.config
    }

    /// Reset ABR state.
    pub fn reset(&mut self) {
        self.recent_bitrates.clear();
        self.frame_count = 0;
        self.total_bytes = 0;
    }

    pub fn frame_count(&self) -> usize {
        self.frame_count
    }

    pub fn total_bytes(&self) -> usize {
        self.total_bytes
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_abr_congestion_response() {
        let mut abr = AdaptiveBitrate::new(5.0);

        // Good network
        let good = NetworkEstimate {
            available_bandwidth_mbps: 20.0,
            packet_loss_rate: 0.001,
            rtt_ms: 10.0,
            jitter_ms: 2.0,
        };
        let cfg_good = abr.adjust(&good).clone();

        // Bad network
        let bad = NetworkEstimate {
            available_bandwidth_mbps: 1.0,
            packet_loss_rate: 0.08,
            rtt_ms: 200.0,
            jitter_ms: 50.0,
        };
        let cfg_bad = abr.adjust(&bad);

        // Bad network should have lower quality settings
        assert!(cfg_bad.gtm_bits_keyframe <= cfg_good.gtm_bits_keyframe);
        assert!(cfg_bad.sparse_fraction <= cfg_good.sparse_fraction);
        assert!(cfg_bad.keyframe_interval <= cfg_good.keyframe_interval);
    }
}
