#!/usr/bin/env python3
"""
OmniQuant-Apex: GPU-Enabled Demo
Safe version - works on PC and Google Colab
"""
import sys
import os

# ============ COLAB FIX ============
# Try to find the project directory
_script_dir = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()

# Common locations for Colab
_possible_paths = [
    _script_dir,
    '/content/omniaquant',
    '/content/omniaquant/New Folder',
    '/content/omniaquant/New Folder-main',
]

project_dir = None
for p in _possible_paths:
    if os.path.exists(p) and os.path.isdir(p):
        # Check if ulep folder exists
        if os.path.exists(os.path.join(p, 'ulep')):
            project_dir = p
            break

# Fallback: search for ulep
if project_dir is None:
    for root, dirs, files in os.walk('/content'):
        if 'ulep' in dirs:
            project_dir = root
            break

if project_dir is None:
    project_dir = _script_dir

os.chdir(project_dir)
sys.path.insert(0, project_dir)
# ============ END COLAB FIX ============

import torch
import numpy as np
from PIL import Image
import time
import gc

print(f"Working directory: {os.getcwd()}")

from ulep.model import ULEP
from mrgwd.model import MRGWD
from codec.encoder import OmniQuantEncoder
from codec.decoder import OmniQuantDecoder
from demo.metrics import compute_psnr, compute_ssim


def generate_test_frame(idx: int, size=(256, 256)) -> Image.Image:
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


def pil_to_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.array(img).astype(np.float32) / 127.5 - 1.0
    return torch.tensor(arr).permute(2, 0, 1)


def tensor_to_pil(t: torch.Tensor) -> Image.Image:
    arr = ((t.clamp(-1, 1) + 1) / 2 * 255).byte().permute(1, 2, 0).cpu().numpy()
    return Image.fromarray(arr)


def main():
    print("=" * 60)
    print("  OmniQuant-Apex: GPU Demo")
    print("  Safe mode - won't crash")
    print("=" * 60)
    print()
    
    LATENT_DIM = 512
    OUTPUT_SIZE = (256, 256)
    N_FRAMES = 30
    
    # Check GPU
    print("[1/4] Checking GPU...")
    device = torch.device("cpu")
    use_vae = False
    
    # Try to detect GPU
    if torch.cuda.is_available():
        device = torch.device("cuda")
        print(f"  Found CUDA GPU: {torch.cuda.get_device_name(0)}")
        use_vae = True
    else:
        # Try ROCm
        try:
            if hasattr(torch.version, 'hip') and torch.version.hip:
                device = torch.device("hip")
                print(f"  Found ROCm GPU")
                use_vae = True
        except:
            pass
    
    # Try OpenCL fallback
    if not use_vae:
        try:
            # Check for AMD GPU via ROCm
            result = os.popen("rocminfo 2>/dev/null | head -20").read()
            if "gfx" in result.lower():
                print("  Found ROCm - would use GPU if PyTorch supports it")
        except:
            pass
    
    print(f"  Device: {device}")
    print(f"  SD-VAE: {'Yes' if use_vae else 'No (falling back to CPU)'}")
    print()
    
    # Build models
    print("[2/4] Building models...")
    print("  Loading DINOv2-small (frozen)...")
    ulep_enc = ULEP(latent_dim=LATENT_DIM, use_pretrained=True)
    ulep_dec = ULEP(latent_dim=LATENT_DIM, use_pretrained=True)
    
    print("  Loading MR-GWD...")
    mrgwd = MRGWD(latent_dim=LATENT_DIM, output_size=OUTPUT_SIZE, use_vae=use_vae, force_vae=use_vae)
    
    # Move to device if possible
    if device.type != "cpu":
        print(f"  Moving models to {device}...")
        ulep_enc = ulep_enc.to(device)
        ulep_dec = ulep_dec.to(device)
        mrgwd = mrgwd.to(device)
    
    ulep_enc.eval()
    ulep_dec.eval()
    mrgwd.eval()
    print("  Models ready!")
    print()
    
    # Build codec
    print("[3/4] Building codec...")
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
    print("  Codec ready!")
    print()
    
    # Run demo
    print(f"[4/4] Running {N_FRAMES}-frame demo...")
    print("-" * 60)
    
    psnr_vals = []
    ssim_vals = []
    bitrates = []
    
    start_time = time.perf_counter()
    
    for i in range(N_FRAMES):
        gc.collect()
        torch.cuda.empty_cache() if torch.cuda.is_available() else None
        
        orig_img = generate_test_frame(i, OUTPUT_SIZE)
        orig_tensor = pil_to_tensor(orig_img)
        
        t0 = time.perf_counter()
        
        try:
            packet_bytes, enc_stats = encoder.encode_frame(orig_img)
            dec_tensor, dec_stats = decoder.decode_packet(packet_bytes)
        except Exception as e:
            print(f"  Error on frame {i}: {e}")
            break
        
        dt = (time.perf_counter() - t0) * 1000
        
        psnr = compute_psnr(orig_tensor, dec_tensor.cpu())
        ssim = compute_ssim(orig_tensor, dec_tensor.cpu())
        bitrate_mbps = (len(packet_bytes) * 8) / (1/30 * 1e6)
        
        psnr_vals.append(psnr)
        ssim_vals.append(ssim)
        bitrates.append(bitrate_mbps)
        
        print(f"  Frame {i:2d} | {'KF' if enc_stats.is_keyframe else 'PF'} | "
              f"{len(packet_bytes):4d}B | PSNR={psnr:.1f}dB | "
              f"SSIM={ssim:.3f} | {bitrate_mbps:.3f}Mbps | {dt:.0f}ms")
        
        if i < 5:
            dec_img = tensor_to_pil(dec_tensor.cpu())
            combined = Image.new("RGB", (OUTPUT_SIZE[0]*2, OUTPUT_SIZE[1]))
            combined.paste(orig_img.resize(OUTPUT_SIZE), (0, 0))
            combined.paste(dec_img, (OUTPUT_SIZE[0], 0))
            combined.save(f"gpu_demo_frame_{i:02d}.png")
    
    elapsed = time.perf_counter() - start_time
    
    # Summary
    print("-" * 60)
    print()
    print("RESULTS:")
    print(f"  Frames:          {N_FRAMES}")
    print(f"  Time:            {elapsed:.1f}s ({N_FRAMES/elapsed:.1f} fps)")
    print(f"  Avg PSNR:        {np.mean(psnr_vals):.2f} dB")
    print(f"  Avg SSIM:        {np.mean(ssim_vals):.4f}")
    print(f"  Avg bitrate:     {np.mean(bitrates):.4f} Mbps")
    print()
    
    if use_vae:
        print("  ✓ Used SD-VAE (GPU mode)")
    else:
        print("  Using ConvFallbackDecoder (CPU mode)")
    
    print()
    print("NOTE: Your AMD BC-250 is an integrated GPU.")
    print("      It may not have enough VRAM for full SD-VAE.")
    print("      For best results, a discrete GPU with 4GB+ VRAM is needed.")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())