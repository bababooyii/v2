import sys, os
sys.path.insert(0, os.getcwd())

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from PIL import Image
import glob
import cv2
import time

print(f"GPU: {torch.cuda.is_available()}")

from mrgwd.model import MRGWD
from gtm.codec import GTMEncoder, GTMDecoder
from demo.metrics import compute_psnr

# ============================================================
# CONFIG - Simple: VAE encoder -> GTM -> VAE decoder
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = 256
OUTPUT_SIZE = 256
BATCH_SIZE = 4
NUM_VIDEOS = 100
FRAMES_PER_VIDEO = 8
NUM_EPOCHS = 10
CHECKPOINT_PATH = "/kaggle/working/omniquant_apex_v8.pt"

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

mrgwd = MRGWD(latent_dim=512, output_size=(OUTPUT_SIZE, OUTPUT_SIZE), use_vae=True, force_vae=True).cuda()

# Freeze VAE encoder and decoder (we're testing GTM quality)
for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

print(f"Trainable params: 0 (testing GTM quality)")

# GTM
gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
gtm_dec = GTMDecoder(qjl_proj_dim=64)

# ============================================================
# LOAD DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Total videos: {len(video_files)}")
train_videos = video_files[:NUM_VIDEOS]
test_videos = video_files[NUM_VIDEOS:NUM_VIDEOS + 20]

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess(img):
    arr = np.array(img.resize((IMAGE_SIZE, IMAGE_SIZE))).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# EVALUATE - VAE encoder -> GTM -> VAE decoder (no training)
# ============================================================
print(f"\n🧪 Evaluating GTM quality (no training)...")

mrgwd.latent_synth.vae.eval()
vae_scale = mrgwd.latent_synth._scale_factor

psnr_vals = []
psnr_no_gtm = []  # Without GTM for comparison

for vi, video in enumerate(test_videos):
    try:
        cap = cv2.VideoCapture(video)
        frames = []
        while len(frames) < 10:
            ret, frame = cap.read()
            if not ret:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(frame))
        cap.release()
        
        for img in frames[:5]:
            x = preprocess(img).unsqueeze(0).cuda()
            target = x.squeeze(0).cpu()
            
            with torch.no_grad():
                # Get VAE latent
                vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                vae_latent_scaled = vae_latent * vae_scale
                z_flat = vae_latent_scaled.squeeze(0).flatten().cuda()
                
                # NO GTM - just VAE encode/decode
                decoded_no_gtm = mrgwd.latent_synth.vae.decode(vae_latent_scaled / vae_scale).sample
                psnr_no_gtm.append(compute_psnr(target, decoded_no_gtm.squeeze(0).cpu()))
                
                # WITH GTM
                z_norm = z_flat / (z_flat.norm() + 1e-8)
                packets = gtm_enc.encode(z_norm.cpu())
                z_dec = gtm_dec.decode(packets, 4096, device='cuda')
                
                dec = mrgwd.latent_synth.vae.decode(z_dec.view(1, 4, 32, 32) / vae_scale).sample
                
                psnr = compute_psnr(target, dec.squeeze(0).cpu())
                psnr_vals.append(psnr)
                
    except Exception as e:
        print(f"Eval error: {e}")
        continue
    
    if (vi + 1) % 5 == 0:
        print(f"  Video {vi+1}/{len(test_videos)}")

print(f"\n📊 PSNR (VAE only, no GTM): {np.mean(psnr_no_gtm):.2f} dB")
print(f"📊 PSNR (VAE + GTM): {np.mean(psnr_vals):.2f} dB (std: {np.std(psnr_vals):.2f})")

print(f"\n💾 Checkpoint: {CHECKPOINT_PATH}")