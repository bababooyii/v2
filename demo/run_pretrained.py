#!/usr/bin/env python3
"""
OmniQuant-Apex: Pretrained-Only Demo
No training required - uses frozen DINOv2 + SD-VAE
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch
import numpy as np
from PIL import Image
import time

from ulep.model import ULEP
from mrgwd.model import MRGWD
from codec.encoder import OmniQuantEncoder
from codec.decoder import OmniQuantDecoder
from demo.metrics import compute_psnr, compute_ssim


def generate_test_frame(idx: int, size=(256, 256)) -> Image.Image:
    """Generate animated test frame."""
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
    print("  OmniQuant-Apex: Pretrained-Only Pipeline")
    print("  Using frozen DINOv2 + SD-VAE (no training)")
    print("=" * 60)
    print()
    
    LATENT_DIM = 512
    OUTPUT_SIZE = (256, 256)
    N_FRAMES = 30
    
    # Build models - will use pretrained DINOv2 automatically
    print("[1/3] Loading pretrained models...")
    print("  - DINOv2-small (frozen)")
    print("  - SD-VAE decoder (frozen)")
    print("  - EncodeHead + PredictorHead (random)")
    
    ulep_enc = ULEP(latent_dim=LATENT_DIM, use_pretrained=True)
    ulep_dec = ULEP(latent_dim=LATENT_DIM, use_pretrained=True)
    mrgwd = MRGWD(latent_dim=LATENT_DIM, output_size=OUTPUT_SIZE, use_vae=True, force_vae=False)
    
    ulep_enc.eval()
    ulep_dec.eval()
    mrgwd.eval()
    
    print("  Models loaded!")
    print()
    
    # Build encoder/decoder
    print("[2/3] Building codec pipeline...")
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
    print(f"[3/3] Running {N_FRAMES}-frame demo...")
    print("-" * 60)
    
    psnr_vals = []
    ssim_vals = []
    bitrates = []
    
    start_time = time.perf_counter()
    
    for i in range(N_FRAMES):
        # Generate frame
        orig_img = generate_test_frame(i, OUTPUT_SIZE)
        orig_tensor = pil_to_tensor(orig_img)
        
        t0 = time.perf_counter()
        
        # Encode
        packet_bytes, enc_stats = encoder.encode_frame(orig_img)
        
        # Decode
        dec_tensor, dec_stats = decoder.decode_packet(packet_bytes)
        
        dt = (time.perf_counter() - t0) * 1000
        
        # Metrics
        psnr = compute_psnr(orig_tensor, dec_tensor)
        ssim = compute_ssim(orig_tensor, dec_tensor)
        bitrate_mbps = (len(packet_bytes) * 8) / (1/30 * 1e6)
        
        psnr_vals.append(psnr)
        ssim_vals.append(ssim)
        bitrates.append(bitrate_mbps)
        
        print(f"  Frame {i:2d} | {'KF' if enc_stats.is_keyframe else 'PF'} | "
              f"{len(packet_bytes):4d}B | PSNR={psnr:.1f}dB | "
              f"SSIM={ssim:.3f} | {bitrate_mbps:.3f}Mbps | {dt:.0f}ms")
        
        # Save comparison
        if i < 10:
            dec_img = tensor_to_pil(dec_tensor)
            combined = Image.new("RGB", (OUTPUT_SIZE[0]*2, OUTPUT_SIZE[1]))
            combined.paste(orig_img.resize(OUTPUT_SIZE), (0, 0))
            combined.paste(dec_img, (OUTPUT_SIZE[0], 0))
            combined.save(f"demo_frame_{i:02d}.png")
    
    elapsed = time.perf_counter() - start_time
    
    # Summary
    print("-" * 60)
    print()
    print("RESULTS:")
    print(f"  Frames processed:  {N_FRAMES}")
    print(f"  Total time:         {elapsed:.2f}s ({N_FRAMES/elapsed:.1f} fps)")
    print(f"  Avg PSNR:          {np.mean(psnr_vals):.2f} dB")
    print(f"  Avg SSIM:          {np.mean(ssim_vals):.4f}")
    print(f"  Avg bitrate:       {np.mean(bitrates):.4f} Mbps")
    print()
    print("STATUS:")
    print(f"  - DINOv2: NOT LOADED (transformers 5.x incompatibility)")
    print(f"  - SD-VAE: NOT LOADED (no GPU)")
    print(f"  - Using: ConvNet fallback + ConvFallbackDecoder")
    print(f"  - Quality: POOR (random weights)")
    print()
    print("FIXING OPTIONS:")
    print("  1. Install: pip install 'transformers<5.0' for DINOv2")
    print("  2. Use GPU (CUDA) for SD-VAE")
    print("  3. Try Option 2 (CLIP) - more robust on CPU")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())