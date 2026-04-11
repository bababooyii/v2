import sys, os
sys.path.insert(0, os.getcwd())

import torch
import numpy as np
from PIL import Image
import glob
import cv2

print(f"GPU: {torch.cuda.is_available()}")

from mrgwd.model import MRGWD
from gtm.codec import GTMEncoder, GTMDecoder
from demo.metrics import compute_psnr

# ============================================================
# CONFIG - Find optimal GTM settings (max 8 bits)
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = 256

# GTM limited to 8 bits max - test various chunk sizes
CHUNK_SIZES = [16, 32, 64]  # Smaller chunks = more chunks = more bits overall
QJL_DIMS = [128, 256, 512]

NUM_VIDEOS = 15
CHECKPOINT_PATH = "/kaggle/working/omniquant_apex_v10.pt"

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

mrgwd = MRGWD(latent_dim=512, output_size=(IMAGE_SIZE, IMAGE_SIZE), use_vae=True, force_vae=True).cuda()

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor

# ============================================================
# LOAD DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Total videos: {len(video_files)}")
test_videos = video_files[200:200 + NUM_VIDEOS]

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess(img):
    arr = np.array(img.resize((IMAGE_SIZE, IMAGE_SIZE))).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# EVALUATE - Test chunk sizes and QJL dims
# ============================================================
print(f"\n🧪 Testing GTM chunk sizes...")

def evaluate(gtm_enc, gtm_dec, test_videos, max_videos=8, max_frames=4):
    psnr_vals = []
    
    for vi, video in enumerate(test_videos[:max_videos]):
        try:
            cap = cv2.VideoCapture(video)
            frames = []
            while len(frames) < max_frames * 2:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame))
            cap.release()
            
            for img in frames[:max_frames]:
                x = preprocess(img).unsqueeze(0).cuda()
                target = x.squeeze(0).cpu()
                
                with torch.no_grad():
                    vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                    vae_latent_scaled = vae_latent * vae_scale
                    z_flat = vae_latent_scaled.squeeze(0).flatten().cuda()
                    
                    z_norm = z_flat / (z_flat.norm() + 1e-8)
                    packets = gtm_enc.encode(z_norm.cpu())
                    z_dec = gtm_dec.decode(packets, 4096, device='cuda')
                    
                    dec = mrgwd.latent_synth.vae.decode(z_dec.view(1, 4, 32, 32) / vae_scale).sample
                    
                    psnr = compute_psnr(target, dec.squeeze(0).cpu())
                    psnr_vals.append(psnr)
                    
        except Exception as e:
            continue
    
    return np.mean(psnr_vals) if psnr_vals else 0

results = []

for chunk_size in CHUNK_SIZES:
    for qjl_dim in QJL_DIMS:
        print(f"\n🔬 Testing chunk_size={chunk_size}, qjl_dim={qjl_dim}")
        
        # Create GTM with custom chunk size by modifying on the fly
        gtm_enc = GTMEncoder(n_bits=8, qjl_proj_dim=qjl_dim, seed=chunk_size)
        gtm_dec = GTMDecoder(qjl_proj_dim=qjl_dim)
        
        # Override chunk size using the chunk_size attribute
        gtm_enc.CHUNK_SIZE = chunk_size
        
        psnr = evaluate(gtm_enc, gtm_dec, test_videos, max_videos=8, max_frames=4)
        
        results.append((chunk_size, qjl_dim, psnr))
        print(f"  -> PSNR: {psnr:.2f} dB")

# Find best
best = max(results, key=lambda x: x[2])
print(f"\n🏆 Best: chunk_size={best[0]}, qjl_dim={best[1]} -> {best[2]:.2f} dB")

print("\n📊 All results (sorted):")
for chunk_size, qjl_dim, psnr in sorted(results, key=lambda x: -x[2]):
    print(f"  chunk_size={chunk_size}, qjl_dim={qjl_dim}: {psnr:.2f} dB")