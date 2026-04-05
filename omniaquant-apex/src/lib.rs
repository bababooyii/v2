//! OmniQuant-Apex: Hyper-Semantic Polar Streaming Codec
//!
//! Ultra-low-bitrate (0.1–0.5 Mbps for 8K), perceptually flawless,
//! and robust video streaming by transmitting only the near-zero entropy
//! of unpredicted semantic change within a global latent space.

pub mod gtm;
pub mod ulep;
pub mod mrgwd;
pub mod codec;
pub mod streaming;
pub mod metrics;
pub mod video_io;
pub mod onnx_engine;
pub mod webrtc;
pub mod pretrained;
