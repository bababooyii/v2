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
# CPU fallback for P100 compatibility
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
# LOAD MODELS ON CPU
# ============================================================
print("\n🤖 Loading models on CPU...")

mrgwd = MRGWD(latent_dim=512, output_size=(1080, 1920), use_vae=True, force_vae=True).cpu()

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor
print("VAE loaded on CPU")

# ============================================================
# TEST
# ============================================================
video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

test_videos = video_files[1000:1010]

output_resolutions = [
    (1920, 1080),
    (2560, 1440),
    (3840, 2160),
    (7680, 4320),
]

print("\n🧪 Testing on CPU (slower but works)...")

for target_res in output_resolutions:
    print(f"\n📺 Target: {target_res[0]}x{target_res[1]}...")
    psnr_vals = []
    
    for vi, video in enumerate(test_videos[:3]):
        try:
            cap = cv2.VideoCapture(video)
            ret, frame = cap.read()
            if not ret:
                cap.release()
                continue
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frame = Image.fromarray(frame)
            cap.release()
            
            x_1080p = preprocess(frame, (1920, 1080)).unsqueeze(0)
            target = preprocess(frame, target_res).unsqueeze(0)
            
            with torch.no_grad():
                vae_latent = mrgwd.latent_synth.vae.encode(x_1080p).latent_dist.sample()
                vae_latent_scaled = vae_latent * vae_scale
                vae_quantized = quantize_latent(vae_latent_scaled, bits=10)
                dec = mrgwd.latent_synth.vae.decode(vae_quantized / vae_scale).sample
                dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
                
                psnr = compute_psnr(target.squeeze(0), dec.squeeze(0))
                psnr_vals.append(psnr)
                
        except Exception as e:
            print(f"  Error: {e}")
            continue
    
    if psnr_vals:
        print(f"  -> PSNR: {np.mean(psnr_vals):.2f} dB")

print("\n✅ Done! (CPU is slower but works on P100)")