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
# CONFIG - 8K with TILING
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"

# 8K resolution
IMAGE_SIZE = (4320, 7680)

# Tiling config - process in chunks to save memory
TILE_SIZE = (1080, 1920)  # Process 1080p tiles
TILE_OVERLAP = 64

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

# Use 1080p model for tiles
mrgwd = MRGWD(latent_dim=512, output_size=TILE_SIZE, use_vae=True, force_vae=True).cuda()

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor
print(f"VAE loaded, tile size: {TILE_SIZE}")

# ============================================================
# LOAD DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

test_videos = video_files[800:805]  # Just 5 videos

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess(img, size):
    arr = np.array(img.resize(size, Image.LANCZOS)).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# TILE PROCESSING
# ============================================================
def process_tiles(image_tensor, tile_size, overlap=64):
    """Process large image in tiles and stitch back"""
    B, C, H, W = image_tensor.shape
    
    # Calculate tiles
    stride_h = tile_size[0] - overlap
    stride_w = tile_size[1] - overlap
    
    n_tiles_h = max(1, (H - overlap - 1) // stride_h + 1)
    n_tiles_w = max(1, (W - overlap - 1) // stride_w + 1)
    
    print(f"  Processing {n_tiles_h}x{n_tiles_w} tiles ({n_tiles_h * n_tiles_w} total)")
    
    # Process each tile
    output_tiles = []
    for i in range(n_tiles_h):
        row_tiles = []
        for j in range(n_tiles_w):
            # Calculate tile bounds
            y_start = min(i * stride_h, H - tile_size[0])
            x_start = min(j * stride_w, W - tile_size[1])
            y_end = y_start + tile_size[0]
            x_end = x_start + tile_size[1]
            
            # Extract tile
            tile = image_tensor[:, :, y_start:y_end, x_start:x_end]
            
            # Process through VAE
            with torch.no_grad():
                vae_latent = mrgwd.latent_synth.vae.encode(tile).latent_dist.sample()
                vae_latent_scaled = vae_latent * vae_scale
                vae_quantized = quantize_latent(vae_latent_scaled, bits=10)
                dec_tile = mrgwd.latent_synth.vae.decode(vae_quantized / vae_scale).sample
            
            row_tiles.append(dec_tile)
        
        # Concatenate row
        if len(row_tiles) > 1:
            row = torch.cat(row_tiles, dim=3)
        else:
            row = row_tiles[0]
        output_tiles.append(row)
    
    # Concatenate all rows
    if len(output_tiles) > 1:
        output = torch.cat(output_tiles, dim=2)
    else:
        output = output_tiles[0]
    
    return output

# ============================================================
# TEST
# ============================================================
print(f"\n🧪 Testing 8K with tiling...")

for vi, video in enumerate(test_videos):
    print(f"\n📺 Video {vi+1}/{len(test_videos)}")
    
    try:
        cap = cv2.VideoCapture(video)
        frames = []
        while len(frames) < 2:
            ret, frame = cap.read()
            if not ret:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(frame))
        cap.release()
        
        for fi, img in enumerate(frames):
            print(f"  Frame {fi+1}: {img.size}")
            
            # Resize to 8K
            img_8k = img.resize((IMAGE_SIZE[1], IMAGE_SIZE[0]), Image.LANCZOS)
            x = preprocess(img_8k, IMAGE_SIZE).unsqueeze(0).cuda()
            target = x.squeeze(0).cpu()
            
            print(f"  Input shape: {x.shape}, {x.numel() * 4 / 1e9:.2f} GB")
            
            # Process in tiles
            with torch.no_grad():
                dec = process_tiles(x, TILE_SIZE, TILE_OVERLAP)
            
            # Resize output to match target (may be slightly different due to tiling)
            if dec.shape != target.shape:
                dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
            
            psnr = compute_psnr(target, dec.squeeze(0).cpu())
            print(f"  -> PSNR: {psnr:.2f} dB")
            
    except Exception as e:
        print(f"  Error: {e}")
        continue

print("\n📊 Done!")
print(f"\nBitrate: 4096 values × 10 bits × {4320*7680 / (1920*1080):.0f} pixels = ~5 KB/frame (scaled)")
print("8K is 4x 4K, 16x 1080p - just multiply base bitrate accordingly")