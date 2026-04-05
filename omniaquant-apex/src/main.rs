//! OmniQuant-Apex CLI — Hyper-Semantic Polar Streaming Codec
//!
//! Usage:
//!   omniaquant-apex encode --input video.mp4 --output encoded.bin
//!   omniaquant-apex decode --input encoded.bin --output decoded.mp4
//!   omniaquant-apex server --port 8000
//!   omniaquant-apex webrtc --port 8000
//!   omniaquant-apex demo --frames 120
//!   omniaquant-apex train --data /path/to/videos

use std::path::PathBuf;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "omniaquant-apex")]
#[command(about = "Hyper-Semantic Polar Streaming Codec")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Encode a video file
    Encode {
        #[arg(short, long)]
        input: PathBuf,

        #[arg(short, long)]
        output: PathBuf,

        #[arg(long, default_value = "60")]
        keyframe_interval: usize,

        #[arg(long, default_value = "0.15")]
        lcc_threshold: f64,

        #[arg(long, default_value = "0.25")]
        sparse_fraction: f64,

        #[arg(long, default_value = "6")]
        gtm_bits_kf: usize,

        #[arg(long, default_value = "3")]
        gtm_bits_pf: usize,

        #[arg(long, default_value = "512")]
        latent_dim: usize,

        #[arg(long)]
        onnx_models: Option<PathBuf>,
    },

    /// Decode an encoded file
    Decode {
        #[arg(short, long)]
        input: PathBuf,

        #[arg(short, long)]
        output: PathBuf,

        #[arg(long, default_value = "512")]
        latent_dim: usize,

        #[arg(long)]
        onnx_models: Option<PathBuf>,
    },

    /// Start the WebSocket streaming server
    Server {
        #[arg(short, long, default_value = "8000")]
        port: u16,

        #[arg(long, default_value = "512")]
        latent_dim: usize,

        #[arg(long, default_value = "512")]
        width: u32,

        #[arg(long, default_value = "512")]
        height: u32,

        #[arg(long)]
        onnx_models: Option<PathBuf>,
    },

    /// Start the WebRTC streaming server (sub-100ms latency)
    WebRTC {
        #[arg(short, long, default_value = "8000")]
        port: u16,

        #[arg(long, default_value = "512")]
        latent_dim: usize,

        #[arg(long, default_value = "512")]
        width: u32,

        #[arg(long, default_value = "512")]
        height: u32,

        #[arg(long, default_value = "50000")]
        min_port: u16,

        #[arg(long, default_value = "60000")]
        max_port: u16,

        #[arg(long)]
        stun_server: Option<String>,
    },

    /// Run a synthetic demo
    Demo {
        #[arg(long, default_value = "120")]
        frames: usize,

        #[arg(long, default_value = "60")]
        keyframe_interval: usize,

        #[arg(long, default_value = "0.15")]
        lcc_threshold: f64,

        #[arg(long, default_value = "512")]
        latent_dim: usize,

        #[arg(long, default_value = "256")]
        width: u32,

        #[arg(long, default_value = "256")]
        height: u32,
    },

    /// Train ULEP + MR-GWD models
    Train {
        #[arg(long)]
        data: PathBuf,

        #[arg(long, default_value = "100")]
        epochs: usize,

        #[arg(long, default_value = "8")]
        batch_size: usize,

        #[arg(long, default_value = "0.0001")]
        lr: f64,

        #[arg(long, default_value = "512")]
        latent_dim: usize,

        #[arg(long, default_value = "224")]
        frame_size: u32,

        #[arg(long, default_value = "256")]
        output_size: u32,

        #[arg(long, default_value = "checkpoints")]
        output_dir: PathBuf,

        #[arg(long, default_value = "onnx_models")]
        onnx_dir: PathBuf,

        #[arg(long, default_value = "4")]
        gradient_accumulation: usize,
    },

    /// Export trained models to ONNX
    Export {
        #[arg(long)]
        checkpoint: PathBuf,

        #[arg(long, default_value = "onnx_models")]
        output_dir: PathBuf,

        #[arg(long, default_value = "512")]
        latent_dim: usize,
    },
}

fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::from_default_env()
                .add_directive("omniaquant_apex=info".parse().unwrap()),
        )
        .init();

    let cli = Cli::parse();

    match cli.command {
        Commands::Encode {
            input, output, keyframe_interval, lcc_threshold,
            sparse_fraction, gtm_bits_kf, gtm_bits_pf, latent_dim, onnx_models,
        } => {
            eprintln!("Encoding: {} → {}", input.display(), output.display());
            if let Some(ref models) = onnx_models {
                eprintln!("Using ONNX models from: {}", models.display());
            }
            eprintln!("Keyframe interval: {}, LCC: {}, Sparse: {}", keyframe_interval, lcc_threshold, sparse_fraction);
            eprintln!("Note: Full encode pipeline requires training + ONNX export first.");
            eprintln!("Run: omniaquant-apex train --data <videos> --epochs 100");
        }

        Commands::Decode { input, output, latent_dim, onnx_models } => {
            eprintln!("Decoding: {} → {}", input.display(), output.display());
            if let Some(ref models) = onnx_models {
                eprintln!("Using ONNX models from: {}", models.display());
            }
            eprintln!("Note: Full decode pipeline requires training + ONNX export first.");
        }

        Commands::Server { port, latent_dim, width, height, onnx_models } => {
            eprintln!("Starting OmniQuant-Apex WebSocket server on port {}...", port);
            eprintln!("Latent dim: {}, Output: {}x{}", latent_dim, width, height);
            if let Some(ref models) = onnx_models {
                eprintln!("ONNX models: {}", models.display());
            }
            eprintln!("Connect to: ws://localhost:{}/ws/stream", port);
            eprintln!("Web UI: http://localhost:{}/", port);

            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                let ulep_enc = omniaquant_apex::ulep::ULEP::new(latent_dim, 384, 42);
                let ulep_dec = omniaquant_apex::ulep::ULEP::new(latent_dim, 384, 42);
                let mrgwd = omniaquant_apex::mrgwd::MRGWD::new(latent_dim, 64, 64, width, height, 42);

                let encoder = omniaquant_apex::codec::OmniQuantEncoder::new(
                    ulep_enc, latent_dim, 60, 0.15, 0.25, 6, 3, 64, 42,
                );
                let decoder = omniaquant_apex::codec::OmniQuantDecoder::new(
                    ulep_dec, mrgwd, latent_dim, 64, 0.25,
                );

                omniaquant_apex::streaming::run_server(encoder, decoder, (width, height), port).await;
            });
        }

        Commands::WebRTC { port, latent_dim, width, height, min_port, max_port, stun_server } => {
            eprintln!("Starting OmniQuant-Apex WebRTC server on port {}...", port);
            eprintln!("Latent dim: {}, Output: {}x{}", latent_dim, width, height);
            eprintln!("UDP port range: {}-{}", min_port, max_port);
            if let Some(ref stun) = stun_server {
                eprintln!("STUN server: {}", stun);
            }
            eprintln!("Target latency: <100ms");
            eprintln!("Signaling: ws://localhost:{}/ws/signaling", port);

            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                use omniaquant_apex::webrtc::{SFU, WebRTCConfig, IceServer, signaling::signaling_router};
                use std::sync::Arc;
                use tokio::sync::Mutex;

                let mut config = WebRTCConfig::default();
                config.port_range = (min_port, max_port);

                if let Some(stun) = stun_server {
                    config.ice_servers = vec![IceServer {
                        urls: vec![format!("stun:{}", stun)],
                    }];
                }

                let (sfu, _rx) = SFU::new(config);
                let sfu = Arc::new(Mutex::new(sfu));

                let app = signaling_router(sfu);

                let addr = format!("0.0.0.0:{}", port);
                let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
                axum::serve(listener, app).await.unwrap();
            });
        }

        Commands::Demo { frames, keyframe_interval, lcc_threshold, latent_dim, width, height } => {
            eprintln!("========================================");
            eprintln!("  OmniQuant-Apex: Hyper-Semantic Polar");
            eprintln!("  Streaming Codec — Demo Mode");
            eprintln!("========================================");
            eprintln!();

            let rt = tokio::runtime::Runtime::new().unwrap();
            rt.block_on(async {
                let ulep_enc = omniaquant_apex::ulep::ULEP::new(latent_dim, 384, 42);
                let ulep_dec = omniaquant_apex::ulep::ULEP::new(latent_dim, 384, 42);
                let mrgwd = omniaquant_apex::mrgwd::MRGWD::new(latent_dim, 64, 64, width, height, 42);

                let mut encoder = omniaquant_apex::codec::OmniQuantEncoder::new(
                    ulep_enc, latent_dim, keyframe_interval, lcc_threshold, 0.25, 6, 3, 64, 42,
                );
                let mut decoder = omniaquant_apex::codec::OmniQuantDecoder::new(
                    ulep_dec, mrgwd, latent_dim, 64, 0.25,
                );
                let mut metrics = omniaquant_apex::metrics::MetricsAccumulator::new(30.0);

                eprintln!("[1/3] Building models... done");
                eprintln!("[2/3] Encoding {} frames...", frames);

                let start = std::time::Instant::now();
                let mut total_bytes = 0usize;
                let mut keyframes = 0usize;
                let mut lcc_triggers = 0usize;

                for i in 0..frames {
                    let pil_frame = omniaquant_apex::streaming::server::generate_demo_frame(i, width, height);

                    let (packet, enc_stats) = encoder.encode_frame(&pil_frame);
                    let (decoded_frame, _dec_stats) = decoder.decode_packet_object(&packet).unwrap();

                    let orig_arr = omniaquant_apex::streaming::server::image_to_array(&pil_frame);
                    let dec_arr = omniaquant_apex::streaming::server::image_to_array(&decoded_frame);
                    let psnr = omniaquant_apex::metrics::compute_psnr(&orig_arr, &dec_arr, 2.0);
                    let ssim = omniaquant_apex::metrics::compute_ssim(&orig_arr, &dec_arr);

                    metrics.update(&orig_arr, &dec_arr, enc_stats.packet_bytes, enc_stats.is_keyframe, enc_stats.lcc_triggered);

                    total_bytes += enc_stats.packet_bytes;
                    if enc_stats.is_keyframe { keyframes += 1; }
                    if enc_stats.lcc_triggered { lcc_triggers += 1; }

                    if i % 10 == 0 || i == frames - 1 {
                        let elapsed = start.elapsed();
                        let fps = (i + 1) as f64 / elapsed.as_secs_f64();
                        let bitrate = metrics.instantaneous_bitrate_mbps();

                        eprintln!("  Frame {:4} | {} | {:5} bytes | PSNR={:.1}dB | SSIM={:.4} | BR={:.3} Mbps | {:.1} fps",
                            i,
                            if enc_stats.is_keyframe { "KF" } else { "PF" },
                            enc_stats.packet_bytes,
                            psnr,
                            ssim,
                            bitrate,
                            fps,
                        );
                    }
                }

                let elapsed = start.elapsed();
                let fps = frames as f64 / elapsed.as_secs_f64();

                eprintln!();
                eprintln!("[3/3] Results:");
                eprintln!("========================================");
                eprintln!("  Frames:                  {}", frames);
                eprintln!("  Avg PSNR:                {:.2} dB", metrics.mean_psnr());
                eprintln!("  Avg SSIM:                {:.4}", metrics.mean_ssim());
                eprintln!("  Avg Bitrate:             {:.4} Mbps", metrics.avg_bitrate_mbps());
                eprintln!("  Total Bytes:             {}", total_bytes);
                eprintln!("  Keyframes:               {}", keyframes);
                eprintln!("  LCC Triggers:            {}", lcc_triggers);
                eprintln!("  Processing Speed:        {:.1} fps", fps);
                eprintln!("  Total Time:              {:.2}s", elapsed.as_secs_f64());
                eprintln!("========================================");
            });
        }

        Commands::Train {
            data, epochs, batch_size, lr, latent_dim,
            frame_size, output_size, output_dir, onnx_dir,
            gradient_accumulation,
        } => {
            eprintln!("========================================");
            eprintln!("  OmniQuant-Apex Training Pipeline");
            eprintln!("========================================");
            eprintln!("  Data: {}", data.display());
            eprintln!("  Epochs: {}", epochs);
            eprintln!("  Batch: {} (effective: {})", batch_size, batch_size * gradient_accumulation);
            eprintln!("  LR: {}", lr);
            eprintln!("  Latent dim: {}", latent_dim);
            eprintln!("  Frame size: {}x{}", frame_size, frame_size);
            eprintln!("  Output size: {}x{}", output_size, output_size);
            eprintln!("  Checkpoints: {}", output_dir.display());
            eprintln!("  ONNX export: {}", onnx_dir.display());
            eprintln!("========================================");
            eprintln!();
            eprintln!("Launching PyTorch training script...");

            let script = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .join("train")
                .join("train_pipeline.py");

            if !script.exists() {
                eprintln!("[!] Training script not found at: {}", script.display());
                eprintln!("    Ensure train/train_pipeline.py exists relative to the project root.");
                return;
            }

            let status = std::process::Command::new("python3")
                .args([
                    script.to_str().unwrap(),
                    "--data", data.to_str().unwrap(),
                    "--epochs", &epochs.to_string(),
                    "--batch-size", &batch_size.to_string(),
                    "--lr", &lr.to_string(),
                    "--latent-dim", &latent_dim.to_string(),
                    "--frame-size", &frame_size.to_string(), &frame_size.to_string(),
                    "--output-size", &output_size.to_string(), &output_size.to_string(),
                    "--output-dir", output_dir.to_str().unwrap(),
                    "--onnx-dir", onnx_dir.to_str().unwrap(),
                    "--gradient-accumulation", &gradient_accumulation.to_string(),
                ])
                .status();

            match status {
                Ok(s) if s.success() => {
                    eprintln!("\nTraining complete! ONNX models exported to: {}", onnx_dir.display());
                }
                Ok(s) => {
                    eprintln!("\nTraining exited with code: {:?}", s.code());
                }
                Err(e) => {
                    eprintln!("\nFailed to launch training: {}", e);
                    eprintln!("Ensure Python 3 + PyTorch are installed.");
                }
            }
        }

        Commands::Export { checkpoint, output_dir, latent_dim } => {
            eprintln!("Exporting checkpoint to ONNX...");
            eprintln!("  Checkpoint: {}", checkpoint.display());
            eprintln!("  Output: {}", output_dir.display());
            eprintln!("  Latent dim: {}", latent_dim);
            eprintln!();
            eprintln!("Use the training script with --output-dir to export ONNX models.");
            eprintln!("Or run: python train/train_pipeline.py --data <dummy> --epochs 1 --output-dir checkpoints --onnx-dir onnx_models");
        }
    }
}
