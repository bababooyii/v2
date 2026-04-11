import sys, os
sys.path.insert(0, os.getcwd())

import torch
import torch.nn.functional as F
import numpy as np
from PIL import Image
import glob
import cv2

print(f"GPU: {torch.cuda.is_available()}")

from mrgwd.model import MRGWD
from demo.metrics import compute_psnr

# ============================================================
# CONFIG - Test simple quantization on VAE latent
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = (1080, 1920)  # 1080p

# Test different bit depths for quantization
BIT_DEPTHS = [8, 10, 12, 16]  # 8-bit = 256 levels, 16-bit = 65536 levels

NUM_VIDEOS = 15
FRAMES_PER_VIDEO = 3

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

mrgwd = MRGWD(latent_dim=512, output_size=IMAGE_SIZE, use_vae=True, force_vae=True).cuda()

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor
print(f"VAE loaded, output: {IMAGE_SIZE}")

# ============================================================
# LOAD DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Total videos: {len(video_files)}")
test_videos = video_files[600:600 + NUM_VIDEOS]

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess(img):
    arr = np.array(img.resize(IMAGE_SIZE)).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# SIMPLE QUANTIZATION
# ============================================================
def quantize_latent(latent, bits):
    """Simple scalar quantization"""
    levels = 2 ** bits
    # Scale to [0, 1]
    latent_min = latent.min()
    latent_max = latent.max()
    latent_scaled = (latent - latent_min) / (latent_max - latent_min + 1e-8)
    # Quantize
    quantized = torch.round(latent_scaled * (levels - 1)) / (levels - 1)
    # De-scale
    return quantized * (latent_max - latent_min) + latent_min

# ============================================================
# EVALUATE
# ============================================================
print(f"\n🧪 Testing simple quantization...")

results = []

for bits in BIT_DEPTHS:
    print(f"\n🔬 Testing {bits}-bit quantization...")
    psnr_vals = []
    psnr_raw = []  # No quantization for comparison
    
    for vi, video in enumerate(test_videos):
        try:
            cap = cv2.VideoCapture(video)
            frames = []
            while len(frames) < FRAMES_PER_VIDEO * 2:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame))
            cap.release()
            
            for img in frames[:FRAMES_PER_VIDEO]:
                x = preprocess(img).unsqueeze(0).cuda()
                target = x.squeeze(0).cpu()
                
                with torch.no_grad():
                    # Get VAE latent
                    vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                    vae_latent_scaled = vae_latent * vae_scale
                    
                    # Raw VAE decode (no quantization)
                    dec_raw = mrgwd.latent_synth.vae.decode(vae_latent_scaled / vae_scale).sample
                    if dec_raw.shape != target.shape:
                        dec_raw = F.interpolate(dec_raw, size=target.shape[-2:], mode='bilinear', align_corners=False)
                    psnr_raw.append(compute_psnr(target, dec_raw.squeeze(0).cpu()))
                    
                    # Quantized latent
                    vae_quantized = quantize_latent(vae_latent_scaled, bits)
                    
                    # Decode quantized
                    dec = mrgwd.latent_synth.vae.decode(vae_quantized / vae_scale).sample
                    if dec.shape != target.shape:
                        dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
                    
                    psnr = compute_psnr(target, dec.squeeze(0).cpu())
                    psnr_vals.append(psnr)
                    
        except Exception as e:
            continue
    
    if psnr_vals:
        avg_raw = np.mean(psnr_raw)
        avg_quant = np.mean(psnr_vals)
        results.append((bits, avg_raw, avg_quant, avg_raw - avg_quant))
        print(f"  -> Raw: {avg_raw:.2f} dB, {bits}-bit: {avg_quant:.2f} dB (loss: {avg_raw - avg_quant:.2f} dB)")

print("\n📊 Summary:")
print(f"Raw VAE: {results[0][1]:.2f} dB")
for bits, raw, quant, loss in results:
    print(f"  {bits}-bit: {quant:.2f} dB (loss: {loss:.2f} dB)")

# Best
best = max(results, key=lambda x: x[2])
print(f"\n🏆 Best: {best[0]}-bit -> {best[2]:.2f} dB")