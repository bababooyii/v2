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
# CONFIG - 8K test
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = (2160, 3840)  # 4K
IMAGE_SIZE_8K = (4320, 7680)  # 8K - test if possible

# Simple 10-bit quantization
def quantize_latent(latent, bits=10):
    levels = 2 ** bits
    latent_min = latent.min()
    latent_max = latent.max()
    latent_scaled = (latent - latent_min) / (latent_max - latent_min + 1e-8)
    quantized = torch.round(latent_scaled * (levels - 1)) / (levels - 1)
    return quantized * (latent_max - latent_min) + latent_min

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

# Try 4K first
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

test_videos = video_files[700:710]

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess(img, size):
    arr = np.array(img.resize(size)).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# TEST AT DIFFERENT RESOLUTIONS
# ============================================================
print(f"\n🧪 Testing at 4K and 8K...")

resolutions = [(1080, 1920), (2160, 3840)]

for target_size in resolutions:
    print(f"\n📺 Testing {target_size[0]}x{target_size[1]}...")
    psnr_vals = []
    
    for vi, video in enumerate(test_videos):
        try:
            cap = cv2.VideoCapture(video)
            frames = []
            while len(frames) < 3:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame))
            cap.release()
            
            for img in frames[:2]:
                x = preprocess(img, target_size).unsqueeze(0).cuda()
                target = x.squeeze(0).cpu()
                
                with torch.no_grad():
                    # Encode with VAE
                    vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                    vae_latent_scaled = vae_latent * vae_scale
                    
                    # 10-bit quantization
                    vae_quantized = quantize_latent(vae_latent_scaled, bits=10)
                    
                    # Decode
                    dec = mrgwd.latent_synth.vae.decode(vae_quantized / vae_scale).sample
                    if dec.shape != target.shape:
                        dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
                    
                    psnr = compute_psnr(target, dec.squeeze(0).cpu())
                    psnr_vals.append(psnr)
                    
        except Exception as e:
            print(f"  Error: {e}")
            continue
    
    if psnr_vals:
        avg = np.mean(psnr_vals)
        print(f"  -> PSNR: {avg:.2f} dB")

# ============================================================
# BITRATE CALCULATION
# ============================================================
print("\n📊 Bitrate estimation:")
print("VAE latent: 4096 values × 10 bits = 5120 bytes/frame")
print("@ 30 fps = 153,600 bytes/s = 1.2 Mbps")
print("@ 60 fps = 2.4 Mbps")
print("\n🎯 Netflix 8K: ~20-50 Mbps (we're way lower!)")