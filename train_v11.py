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
# CONFIG - Test VAE at higher resolutions
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"

# Test resolutions
RESOLUTIONS = [
    (256, 256),
    (512, 512),
    (768, 768),
    (1080, 1920),  # 1080p
    # (2160, 3840),  # 4K - too slow for testing
]

NUM_VIDEOS = 10
FRAMES_PER_VIDEO = 5

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

# Use the highest resolution model
mrgwd = MRGWD(latent_dim=512, output_size=(1080, 1920), use_vae=True, force_vae=True).cuda()

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor
print(f"VAE loaded, output size: {mrgwd.output_size}")

# ============================================================
# LOAD DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Total videos: {len(video_files)}")
test_videos = video_files[500:500 + NUM_VIDEOS]

# ============================================================
# PREPROCESSING - Different sizes
# ============================================================
def preprocess(img, size):
    arr = np.array(img.resize(size)).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# EVALUATE - VAE encode/decode at different resolutions
# ============================================================
print(f"\n🧪 Testing VAE at different resolutions...")

results = []

for res in RESOLUTIONS:
    print(f"\n📺 Testing {res[0]}x{res[1]}...")
    psnr_vals = []
    ssim_vals = []
    
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
                # Resize to target resolution
                x = preprocess(img, res).unsqueeze(0).cuda()
                target = x.squeeze(0).cpu()
                
                with torch.no_grad():
                    # VAE encode/decode
                    vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                    vae_latent_scaled = vae_latent * vae_scale
                    
                    # Decode - the upsample_net will handle resizing
                    dec = mrgwd.latent_synth.vae.decode(vae_latent_scaled / vae_scale).sample
                    
                    # If decoded is smaller, upsample it
                    if dec.shape[-2:] != target.shape[-2:]:
                        dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
                    
                    psnr = compute_psnr(target, dec.squeeze(0).cpu())
                    psnr_vals.append(psnr)
                    
        except Exception as e:
            print(f"  Error: {e}")
            continue
    
    if psnr_vals:
        avg_psnr = np.mean(psnr_vals)
        results.append((res, avg_psnr))
        print(f"  -> PSNR: {avg_psnr:.2f} dB (std: {np.std(psnr_vals):.2f})")

print("\n📊 Summary:")
for res, psnr in results:
    print(f"  {res[0]}x{res[1]}: {psnr:.2f} dB")

# Best result
best = max(results, key=lambda x: x[1])
print(f"\n🏆 Best: {best[0][0]}x{best[0][1]} -> {best[1]:.2f} dB")