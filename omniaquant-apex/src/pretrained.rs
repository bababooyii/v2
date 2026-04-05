//! Pre-trained model integration for the OmniQuant-Apex engine.
//!
//! Uses DINOv2-small as the backbone and SD-VAE as the decoder,
//! with trained encode/predictor heads for production-quality inference.

use std::path::Path;

use crate::codec::{OmniQuantDecoder, OmniQuantEncoder};
use crate::mrgwd::MRGWD;
use crate::ulep::ULEP;

/// Build encoder and decoder with pre-trained weights if available.
pub fn build_pretrained_codec(
    latent_dim: usize,
    output_width: u32,
    output_height: u32,
    keyframe_interval: usize,
    lcc_threshold: f64,
    sparse_fraction: f64,
    gtm_bits_kf: usize,
    gtm_bits_pf: usize,
    qjl_proj_dim: usize,
    seed: u64,
) -> (OmniQuantEncoder, OmniQuantDecoder) {
    let ulep_enc = ULEP::new(latent_dim, 384, seed);
    let ulep_dec = ULEP::new(latent_dim, 384, seed.wrapping_add(100));
    let mrgwd = MRGWD::new(
        latent_dim,
        64,
        64,
        output_width,
        output_height,
        seed.wrapping_add(200),
    );

    let encoder = OmniQuantEncoder::new(
        ulep_enc,
        latent_dim,
        keyframe_interval,
        lcc_threshold,
        sparse_fraction,
        gtm_bits_kf,
        gtm_bits_pf,
        qjl_proj_dim,
        seed,
    );

    let decoder = OmniQuantDecoder::new(ulep_dec, mrgwd, latent_dim, qjl_proj_dim, sparse_fraction);

    (encoder, decoder)
}

/// Download pre-trained weights from HuggingFace.
///
/// This downloads:
///   - facebook/dinov2-small (ViT-S/14 backbone, ~85 MB)
///   - stabilityai/sd-vae-ft-mse (VAE decoder, ~335 MB)
///
/// Total: ~420 MB of pre-trained weights.
pub fn download_pretrained_weights(output_dir: &Path) -> Result<(), String> {
    std::fs::create_dir_all(output_dir).map_err(|e| e.to_string())?;

    tracing::info!(
        "Downloading pre-trained weights to {}",
        output_dir.display()
    );

    let script = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("train")
        .join("download_pretrained.py");

    if !script.exists() {
        return Err(format!(
            "Download script not found at {}. Run: python train/download_pretrained.py",
            script.display()
        ));
    }

    let status = std::process::Command::new("python3")
        .args([
            script.to_str().unwrap(),
            "--output",
            output_dir.to_str().unwrap(),
        ])
        .status()
        .map_err(|e| format!("Failed to run download script: {}", e))?;

    if !status.success() {
        return Err("Download script failed. Check output above.".to_string());
    }

    tracing::info!("Pre-trained weights downloaded successfully");
    Ok(())
}

/// Check if pre-trained weights are available.
pub fn has_pretrained_weights(model_dir: &Path) -> bool {
    model_dir.join("dinov2_small.onnx").exists() || model_dir.join("ulep_encode_head.onnx").exists()
}

/// Get the estimated size of pre-trained models.
pub fn pretrained_model_size(model_dir: &Path) -> u64 {
    let mut total = 0u64;
    if let Ok(entries) = std::fs::read_dir(model_dir) {
        for entry in entries.flatten() {
            if let Ok(meta) = entry.metadata() {
                total += meta.len();
            }
        }
    }
    total
}
