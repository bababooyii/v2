//! OmniQuant-Apex Codec — Encoder and Decoder pipelines.

pub mod packets;
pub mod sparse;
pub mod lcc;
pub mod encoder;
pub mod decoder;

pub use encoder::OmniQuantEncoder;
pub use decoder::OmniQuantDecoder;
pub use packets::{Packet, KeyframePacket, PredictivePacket};
pub use lcc::LCC;
pub use sparse::SparseCoder;
