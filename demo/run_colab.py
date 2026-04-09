#!/usr/bin/env python3
"""
OmniQuant-Apex: GPU-Enabled Demo - Fixed for Colab
"""
import sys
import os

# Get the correct project directory
project_dir = os.getcwd()
print(f"Working directory: {project_dir}")

# Check what's in the current directory
print(f"\nContents: {os.listdir(project_dir)}")

# Find the actual project directory (it might be New Folder inside omniaquant)
if 'ulep' not in os.listdir(project_dir) and 'New Folder' in os.listdir('/content/omniaquant'):
    project_dir = '/content/omniaquant/New Folder'
    os.chdir(project_dir)
    print(f"Changed to: {project_dir}")

sys.path.insert(0, project_dir)
print(f"Added to sys.path: {project_dir}")

# Now import and run
print("\n🔄 Loading models...")
import torch

print(f"GPU available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name(0)}")

# Import your modules
from ulep.model import ULEP
from mrgwd.model import MRGWD
from codec.encoder import OmniQuantEncoder
from codec.decoder import OmniQuantDecoder
from demo.metrics import compute_psnr, compute_ssim
import numpy as np
from PIL import Image

print("✅ Imports successful!")

# Generate test frame
def generate_test_frame(idx, size=(256, 256)):
    W, H = size
    t = idx / 60.0
    arr = np.zeros((H, W, 3), dtype=np.uint8)
    xs = np.linspace(0, 1, W)
    ys = np.linspace(0, 1, H)
    X, Y = np.meshgrid(xs, ys)
    r = np.sin(2 * np.pi * (X + t)) * 0.5 + 0.5
    g = np.cos(2 * np.pi * (Y + t * 1.3)) * 0.5 + 0.5
    b = np.sin(2 * np.pi * (X * 0.7 + Y * 0.3 + t * 0.7)) * 0.5 + 0.5
    arr[:, :, 0] = (r * 220).astype(np.uint8)
    arr[:, :, 1] = (g * 180).astype(np.uint8)
    arr[:, :, 2] = (b * 200).astype(np.uint8)
    cx, cy = int(W * (0.5 + 0.35 * np.cos(2 * np.pi * t))), int(H * (0.5 + 0.35 * np.sin(2 * np.pi * t * 1.1)))
    yy, xx = np.ogrid[:H, :W]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 < 3600
    arr[mask, 0] = 255
    arr[mask, 1] = int(200 * (1 - t % 1))
    arr[mask, 2] = int(100 * (t % 1))
    return Image.fromarray(arr)

def pil_to_tensor(img):
    arr = np.array(img).astype(np.float32) / 127.5 - 1.0
    return torch.tensor(arr).permute(2, 0, 1)

# Build models
LATENT_DIM = 512
OUTPUT_SIZE = (256, 256)
N_FRAMES = 10

print("\n🔄 Building models...")
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
use_vae = torch.cuda.is_available()

ulep_enc = ULEP(latent_dim=LATENT_DIM, use_pretrained=True)
ulep_dec = ULEP(latent_dim=LATENT_DIM, use_pretrained=True)
mrgwd = MRGWD(latent_dim=LATENT_DIM, output_size=OUTPUT_SIZE, use_vae=use_vae, force_vae=use_vae)

if torch.cuda.is_available():
    ulep_enc = ulep_enc.cuda()
    ulep_dec = ulep_dec.cuda()
    mrgwd = mrgwd.cuda()

ulep_enc.eval()
ulep_dec.eval()
mrgwd.eval()

print(f"✅ Models loaded! Device: {device}, VAE: {use_vae}")

# Build codec
encoder = OmniQuantEncoder(
    ulep=ulep_enc,
    latent_dim=LATENT_DIM,
    keyframe_interval=30,
    lcc_threshold=0.15,
    sparse_fraction=0.25,
    gtm_bits_keyframe=6,
    gtm_bits_predictive=3,
)
decoder = OmniQuantDecoder(
    ulep=ulep_dec,
    mrgwd=mrgwd,
    latent_dim=LATENT_DIM,
    sparse_fraction=0.25,
)

print("✅ Codec ready!")

# Run demo
print(f"\n🚀 Running {N_FRAMES}-frame demo...")
print("-" * 60)

psnr_vals = []
import time

for i in range(N_FRAMES):
    orig_img = generate_test_frame(i, OUTPUT_SIZE)
    orig_tensor = pil_to_tensor(orig_img)
    if torch.cuda.is_available():
        orig_tensor = orig_tensor.cuda()
    
    t0 = time.time()
    packet_bytes, enc_stats = encoder.encode_frame(orig_img)
    dec_tensor, dec_stats = decoder.decode_packet(packet_bytes)
    dt = (time.time() - t0) * 1000
    
    psnr = compute_psnr(orig_tensor.cpu(), dec_tensor.cpu())
    psnr_vals.append(psnr)
    bitrate_mbps = (len(packet_bytes) * 8) / (1/30 * 1e6)
    
    print(f"  Frame {i:2d} | {'KF' if enc_stats.is_keyframe else 'PF'} | "
          f"{len(packet_bytes):4d}B | PSNR={psnr:.1f}dB | {dt:.0f}ms")

print("-" * 60)
print(f"\n📊 RESULTS:")
print(f"  Avg PSNR: {np.mean(psnr_vals):.2f} dB")
print(f"  Bitrate:  {np.mean([(len(packet_bytes) * 8 / (1/30 * 1e6)) for _ in range(N_FRAMES)]):.4f} Mbps")
print(f"  GPU:      {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}")
print(f"  VAE:      {'YES' if use_vae else 'NO (CPU fallback)'}")

if use_vae:
    print("\n🎉 WITH SD-VAE - You should see ~30-40 dB PSNR (Netflix quality!)")
else:
    print("\n⚠️  Without GPU - using CPU fallback (lower quality)")