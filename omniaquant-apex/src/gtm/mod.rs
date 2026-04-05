//! Global TurboQuant Module (GTM)
//!
//! Applies RHT → Polar Transform → Lloyd-Max Quantization → QJL Bias Correction
//! to latent vectors for ultra-efficient compression.

pub mod rht;
pub mod polar;
pub mod quantize;
pub mod qjl;
pub mod codec;

pub use rht::RHT;
pub use polar::{polar_transform, polar_inverse};
pub use quantize::LloydMaxQuantizer;
pub use qjl::QJL;
pub use codec::{GTMEncoder, GTMDecoder, GTMPacket};
