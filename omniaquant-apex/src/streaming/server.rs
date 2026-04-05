//! WebSocket Streaming Server
//!
//! Axum server with WebSocket endpoints for live encoder/decoder streaming.

use std::sync::Arc;
use tokio::sync::Mutex;
use futures::StreamExt;
use axum::{
    Router,
    extract::{WebSocketUpgrade, State},
    response::IntoResponse,
    routing::{get, post},
    Json,
};
use serde::{Serialize, Deserialize};
use image::{RgbImage};
use ndarray::Array1;

use crate::ulep::ULEP;
use crate::mrgwd::MRGWD;
use crate::codec::{OmniQuantEncoder, OmniQuantDecoder};
use crate::metrics::MetricsAccumulator;
use crate::streaming::adaptive::AdaptiveBitrate;

/// Shared application state.
pub struct AppState {
    pub encoder: Mutex<OmniQuantEncoder>,
    pub decoder: Mutex<OmniQuantDecoder>,
    pub metrics: Mutex<MetricsAccumulator>,
    pub abr: Mutex<AdaptiveBitrate>,
    pub frame_idx: Mutex<usize>,
    pub output_size: (u32, u32),
}

/// Frame response for WebSocket.
#[derive(Serialize)]
pub struct FrameResponse {
    pub frame_idx: usize,
    pub is_keyframe: bool,
    pub is_concealed: bool,
    pub lcc_triggered: bool,
    pub packet_bytes: usize,
    pub bitrate_mbps: f64,
    pub psnr: f64,
    pub ssim: f64,
    pub total_frames: usize,
    pub keyframes: usize,
    pub lcc_triggers: usize,
    pub delta_z_norm: f64,
    pub energy_retention: f64,
    pub sparse_k: usize,
    pub decoded_frame: String,
}

/// Config update request.
#[derive(Deserialize)]
pub struct ConfigUpdate {
    pub keyframe_interval: Option<usize>,
    pub lcc_threshold: Option<f64>,
    pub sparse_fraction: Option<f64>,
    pub gtm_bits_kf: Option<usize>,
    pub gtm_bits_pf: Option<usize>,
    pub packet_loss_rate: Option<f64>,
}

/// Status response.
#[derive(Serialize)]
pub struct StatusResponse {
    pub frames: usize,
    pub avg_psnr_db: f64,
    pub avg_ssim: f64,
    pub avg_bitrate_mbps: f64,
    pub keyframes: usize,
    pub lcc_triggers: usize,
    pub total_bytes: usize,
}

/// Generate an animated synthetic frame for browser demo.
pub fn generate_demo_frame(frame_idx: usize, width: u32, height: u32) -> image::DynamicImage {
    let t = (frame_idx % 300) as f64 / 300.0;
    let mut img = RgbImage::new(width, height);

    let wf = width as f64;
    let hf = height as f64;

    let cx = (wf * (0.5 + 0.35 * (2.0 * std::f64::consts::PI * t).cos())) as u32;
    let cy = (hf * (0.5 + 0.35 * (2.0 * std::f64::consts::PI * t * 1.1).sin())) as u32;

    for y in 0..height {
        for x in 0..width {
            let xf = x as f64 / wf;
            let yf = y as f64 / hf;

            let r = ((0.5 + 0.5 * (2.0 * std::f64::consts::PI * (xf + t)).sin()) * 220.0) as u8;
            let g = ((0.5 + 0.5 * (2.0 * std::f64::consts::PI * (yf + t * 1.3)).cos()) * 180.0) as u8;
            let b = ((0.5 + 0.5 * (2.0 * std::f64::consts::PI * (xf * 0.7 + yf * 0.3 + t * 0.7)).sin()) * 200.0) as u8;

            let dist = ((x as i64 - cx as i64).pow(2) + (y as i64 - cy as i64).pow(2)) as f64;
            if dist < 3600.0 {
                img.put_pixel(x, y, image::Rgb([255, (200.0 * (1.0 - t)) as u8, (100.0 * t) as u8]));
            } else {
                img.put_pixel(x, y, image::Rgb([r, g, b]));
            }
        }
    }

    image::DynamicImage::ImageRgb8(img)
}

/// Convert DynamicImage to base64 JPEG.
pub fn image_to_base64_jpeg(img: &image::DynamicImage, _quality: u8) -> String {
    let mut buf = Vec::new();
    img.write_to(&mut std::io::Cursor::new(&mut buf), image::ImageFormat::Jpeg)
        .unwrap();
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(&buf)
}

/// Convert DynamicImage to Array1 for metrics.
pub fn image_to_array(img: &image::DynamicImage) -> Array1<f64> {
    let rgb = img.to_rgb8();
    let (w, h) = rgb.dimensions();
    let mut data = Vec::with_capacity((w * h * 3) as usize);
    for y in 0..h {
        for x in 0..w {
            let p = rgb.get_pixel(x, y);
            data.push(p[0] as f64 / 127.5 - 1.0);
            data.push(p[1] as f64 / 127.5 - 1.0);
            data.push(p[2] as f64 / 127.5 - 1.0);
        }
    }
    Array1::from_vec(data)
}

/// Compute PSNR between two images.
pub fn compute_psnr_img(a: &image::DynamicImage, b: &image::DynamicImage) -> f64 {
    let a_rgb = a.to_rgb8();
    let b_rgb = b.to_rgb8();
    let w = a_rgb.width().min(b_rgb.width());
    let h = a_rgb.height().min(b_rgb.height());

    let mut mse = 0.0f64;
    let mut count = 0u64;
    for y in 0..h {
        for x in 0..w {
            for c in 0..3 {
                let diff = a_rgb.get_pixel(x, y)[c] as f64 - b_rgb.get_pixel(x, y)[c] as f64;
                mse += diff * diff;
                count += 1;
            }
        }
    }

    let count = count as f64;
    if count == 0.0 || mse < 1e-12 {
        return f64::INFINITY;
    }
    mse /= count;
    10.0 * (255.0f64.powi(2) / mse).log10()
}

/// WebSocket streaming handler.
async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

async fn handle_socket(socket: axum::extract::ws::WebSocket, state: Arc<AppState>) {
    use tokio::time::{interval, Duration};

    let (mut sender, mut _receiver) = socket.split();
    let mut interval = interval(Duration::from_millis(33));
    let mut frame_idx: usize = 0;

    loop {
        interval.tick().await;

        let pil_frame = generate_demo_frame(frame_idx, state.output_size.0, state.output_size.1);

        let (packet, enc_stats) = {
            let mut enc = state.encoder.lock().await;
            enc.encode_frame(&pil_frame)
        };

        let (decoded_frame, _dec_stats) = {
            let mut dec = state.decoder.lock().await;
            dec.decode_packet_object(&packet).unwrap_or_else(|| {
                let mut dec2 = state.decoder.blocking_lock();
                dec2.conceal_one_frame().unwrap()
            })
        };

        {
            let mut metrics = state.metrics.lock().await;
            let orig_arr = image_to_array(&pil_frame);
            let dec_arr = image_to_array(&decoded_frame);
            metrics.update(&orig_arr, &dec_arr, enc_stats.packet_bytes, enc_stats.is_keyframe, enc_stats.lcc_triggered);
        }

        {
            let mut abr = state.abr.lock().await;
            abr.update(enc_stats.packet_bytes, 30.0);
        }

        let dec_b64 = image_to_base64_jpeg(&decoded_frame, 85);
        let psnr = compute_psnr_img(&pil_frame, &decoded_frame);

        let metrics = state.metrics.lock().await;
        let summary = metrics.summary();

        let response = FrameResponse {
            frame_idx,
            is_keyframe: enc_stats.is_keyframe,
            is_concealed: false,
            lcc_triggered: enc_stats.lcc_triggered,
            packet_bytes: enc_stats.packet_bytes,
            bitrate_mbps: metrics.instantaneous_bitrate_mbps(),
            psnr,
            ssim: summary.get("avg_ssim").copied().unwrap_or(0.0),
            total_frames: summary.get("frames").copied().unwrap_or(0.0) as usize,
            keyframes: summary.get("keyframes").copied().unwrap_or(0.0) as usize,
            lcc_triggers: summary.get("lcc_triggers").copied().unwrap_or(0.0) as usize,
            delta_z_norm: enc_stats.delta_z_norm,
            energy_retention: enc_stats.energy_retention,
            sparse_k: enc_stats.sparse_k,
            decoded_frame: dec_b64,
        };

        let msg = serde_json::to_string(&response).unwrap();
        let msg_text = axum::extract::ws::Message::Text(msg.into());
        if futures::SinkExt::send(&mut sender, msg_text).await.is_err() {
            break;
        }

        frame_idx += 1;
    }
}

/// Status endpoint.
async fn get_status(State(state): State<Arc<AppState>>) -> Json<StatusResponse> {
    let metrics = state.metrics.lock().await;
    let summary = metrics.summary();

    Json(StatusResponse {
        frames: summary.get("frames").copied().unwrap_or(0.0) as usize,
        avg_psnr_db: summary.get("avg_psnr_db").copied().unwrap_or(0.0),
        avg_ssim: summary.get("avg_ssim").copied().unwrap_or(0.0),
        avg_bitrate_mbps: summary.get("avg_bitrate_mbps").copied().unwrap_or(0.0),
        keyframes: summary.get("keyframes").copied().unwrap_or(0.0) as usize,
        lcc_triggers: summary.get("lcc_triggers").copied().unwrap_or(0.0) as usize,
        total_bytes: summary.get("total_bytes").copied().unwrap_or(0.0) as usize,
    })
}

/// Config update endpoint.
async fn update_config(
    State(state): State<Arc<AppState>>,
    Json(body): Json<ConfigUpdate>,
) -> Json<serde_json::Value> {
    let mut enc = state.encoder.lock().await;
    if let Some(threshold) = body.lcc_threshold {
        enc.set_lcc_threshold(threshold);
    }
    if let Some(fraction) = body.sparse_fraction {
        enc.set_sparse_fraction(fraction);
    }

    Json(serde_json::json!({"ok": true}))
}

/// Reset endpoint.
async fn reset_codec(State(state): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let mut enc = state.encoder.lock().await;
    let mut dec = state.decoder.lock().await;
    let mut metrics = state.metrics.lock().await;
    let mut abr = state.abr.lock().await;
    let mut frame_idx = state.frame_idx.lock().await;

    enc.reset();
    dec.reset();
    metrics.reset();
    abr.reset();
    *frame_idx = 0;

    Json(serde_json::json!({"ok": true}))
}

/// Build and run the Axum server.
pub async fn run_server(
    encoder: OmniQuantEncoder,
    decoder: OmniQuantDecoder,
    output_size: (u32, u32),
    port: u16,
) {
    let state = Arc::new(AppState {
        encoder: Mutex::new(encoder),
        decoder: Mutex::new(decoder),
        metrics: Mutex::new(MetricsAccumulator::new(30.0)),
        abr: Mutex::new(AdaptiveBitrate::new(5.0)),
        frame_idx: Mutex::new(0),
        output_size,
    });

    let app = Router::new()
        .route("/ws/stream", get(ws_handler))
        .route("/api/status", get(get_status))
        .route("/api/config", post(update_config))
        .route("/api/reset", post(reset_codec))
        .with_state(state);

    let addr = format!("0.0.0.0:{}", port);
    tracing::info!("Starting OmniQuant-Apex server on {}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
