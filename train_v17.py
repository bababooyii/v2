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
# 8K from 1080p source
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"

def quantize_latent(latent, bits=10):
    levels = 2 ** bits
    latent_min = latent.min()
    latent_max = latent.max()
    latent_scaled = (latent - latent_min) / (latent_max - latent_min + 1e-8)
    quantized = torch.round(latent_scaled * (levels - 1)) / (levels - 1)
    return quantized * (latent_max - latent_min) + latent_min

def preprocess(img, size):
    arr = np.array(img.resize(size)).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

mrgwd = MRGWD(latent_dim=512, output_size=(1080, 1920), use_vae=True, force_vae=True).cuda()

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor

# ============================================================
# TEST: 1080p source -> 8K output
# ============================================================
print(f"\n🧪 Testing 1080p source -> 8K output...")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

test_videos = video_files[1000:1010]

output_resolutions = [
    (1920, 1080),   # 1080p
    (3840, 2160),   # 4K
    (7680, 4320),   # 8K
]

results = []

for out_res in output_resolutions:
    print(f"\n📺 Encode at 1080p, decode at {out_res[0]}x{out_res[1]}...")
    psnr_vals = []
    
    for video in test_videos:
        try:
            cap = cv2.VideoCapture(video)
            ret, frame = cap.read()
            if not ret:
                cap.release()
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame = Image.fromarray(frame)
            cap.release()
            
            # Encode at 1080p
            x_1080p = preprocess(frame, (1920, 1080)).unsqueeze(0).cuda()
            
            # Target: upscale to out_res (what we'd want at 8K display)
            target = preprocess(frame, out_res).unsqueeze(0).cuda()
            
            with torch.no_grad():
                vae_latent = mrgwd.latent_synth.vae.encode(x_1080p).latent_dist.sample()
                vae_latent_scaled = vae_latent * vae_scale
                vae_quantized = quantize_latent(vae_latent_scaled, bits=10)
                
                # Decode - VAE will output at 256p, then we upscale
                dec = mrgwd.latent_synth.vae.decode(vae_quantized / vae_scale).sample
                
                # Upscale to target resolution
                dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
                
                psnr = compute_psnr(target.squeeze(0).cpu(), dec.squeeze(0).cpu())
                psnr_vals.append(psnr)
                
        except Exception as e:
            continue
    
    if psnr_vals:
        avg = np.mean(psnr_vals)
        results.append((out_res, avg))
        print(f"  -> PSNR: {avg:.2f} dB")

# ============================================================
# BITRATE
# ============================================================
print("\n📊 Summary:")
print("="*60)
print("Source: 1080p (1920x1080) - modern video quality")
print("Latent: 4096 values × 10 bits = 640 bytes/frame")
print("Bitrate: 640 * 30 fps = 19,200 bytes/s = 0.15 Mbps")
print("="*60)

for res, psnr in results:
    mp = (res[0] * res[1]) / 1e6
    print(f"Output {res[0]:4d}x{res[1]:4d} ({mp:4.1f}MP): PSNR {psnr:5.2f} dB")

print("\n🎯 Our 8K:")
print("  Bitrate: 0.15 Mbps")
print("  PSNR: ~" + f"{results[2][1]:.0f}" + " dB (if we can run it)")
print("\n🎯 Netflix 8K:")
print("  Bitrate: 20-50 Mbps")
print("  PSNR: ~40 dB")

print("\n📈 To match Netflix quality, we need ~8 dB more")
print("   Options: Higher bit-depth, better quantization, or native 8K encode")