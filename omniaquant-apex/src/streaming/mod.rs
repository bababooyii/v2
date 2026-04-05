//! Streaming layer — adaptive bitrate, transport, and WebSocket server.

pub mod adaptive;
pub mod server;

pub use adaptive::AdaptiveBitrate;
pub use server::run_server;
