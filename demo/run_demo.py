#!/usr/bin/env python3
"""
OmniQuant-Apex CLI Demo

Encodes and decodes a video file, displaying per-frame metrics and
saving a side-by-side comparison video.

Usage:
    python demo/run_demo.py --video path/to/video.mp4 [options]
    python demo/run_demo.py --webcam [options]
    python demo/run_demo.py --synthetic [options]   # generate test frames
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import torch
import numpy as np
from PIL import Image
from tqdm import tqdm
import time

from ulep.model import ULEP
from mrgwd.model import MRGWD
from codec.encoder import OmniQuantEncoder
from codec.decoder import OmniQuantDecoder
from codec.packets import serialize_packet, deserialize_packet
from demo.metrics import MetricsAccumulator, compute_bitrate


def load_video_frames(path: str, max_frames: int = 300, size=(512, 512)):
    """Load video frames from file using OpenCV."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        frames = []
        while len(frames) < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil = Image.fromarray(frame).resize(size, Image.LANCZOS)
            frames.append(pil)
        cap.release()
        return frames
    except Exception as e:
        print(f"[!] OpenCV not available or video error: {e}")
        return None


def generate_synthetic_frames(n: int = 120, size=(512, 512)):
    """Generate animated synthetic frames for testing."""
    frames = []
    W, H = size
    for i in range(n):
        arr = np.zeros((H, W, 3), dtype=np.uint8)
        t = i / n
        # Moving gradient + shape
        for y in range(H):
            for x in range(W):
                arr[y, x, 0] = int(255 * (x / W + 0.3 * np.sin(2 * np.pi * t + y / H)))
                arr[y, x, 1] = int(255 * (y / H + 0.3 * np.cos(2 * np.pi * t)))
                arr[y, x, 2] = int(255 * t)
        arr = arr.clip(0, 255)
        frames.append(Image.fromarray(arr))
    return frames


def tensor_to_pil(t: torch.Tensor, size=None) -> Image.Image:
    """Convert (3,H,W) tensor in [-1,1] to PIL image."""
    arr = ((t.clamp(-1, 1) + 1) / 2 * 255).byte().permute(1, 2, 0).cpu().numpy()
    img = Image.fromarray(arr)
    if size:
        img = img.resize(size, Image.LANCZOS)
    return img


def pil_to_tensor(img: Image.Image) -> torch.Tensor:
    """Convert PIL image to (3,H,W) tensor in [-1,1]."""
    arr = np.array(img).astype(np.float32) / 127.5 - 1.0
    return torch.tensor(arr).permute(2, 0, 1)


def main():
    parser = argparse.ArgumentParser(description="OmniQuant-Apex Demo")
    parser.add_argument("--video", type=str, default=None, help="Input video file")
    parser.add_argument("--webcam", action="store_true", help="Use webcam")
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic test frames")
    parser.add_argument("--max-frames", type=int, default=120, help="Max frames to encode")
    parser.add_argument("--keyframe-interval", type=int, default=30)
    parser.add_argument("--lcc-threshold", type=float, default=0.15)
    parser.add_argument("--sparse-fraction", type=float, default=0.25)
    parser.add_argument("--gtm-bits-kf", type=int, default=6)
    parser.add_argument("--gtm-bits-pf", type=int, default=3)
    parser.add_argument("--latent-dim", type=int, default=512)
    parser.add_argument("--output-size", type=int, nargs=2, default=[256, 256])
    parser.add_argument("--output-video", type=str, default=None)
    parser.add_argument("--mode", choices=["interactive", "headless"], default="headless")
    parser.add_argument("--no-pretrained", action="store_true", help="Skip downloading DINOv2")
    args = parser.parse_args()

    print("=" * 60)
    print("  OmniQuant-Apex: Hyper-Semantic Polar Streaming Codec")
    print("=" * 60)

    use_pretrained = not args.no_pretrained
    output_size = tuple(args.output_size)

    # Load frames
    print("\n[1/4] Loading frames...")
    frames = None
    if args.video:
        frames = load_video_frames(args.video, max_frames=args.max_frames, size=output_size)
    if frames is None or args.synthetic:
        print("  Using synthetic test frames...")
        frames = generate_synthetic_frames(n=args.max_frames, size=output_size)
    print(f"  Loaded {len(frames)} frames at {output_size[0]}x{output_size[1]}")

    # Build models
    print("\n[2/4] Building models...")
    ulep_enc = ULEP(latent_dim=args.latent_dim, use_pretrained=use_pretrained)
    ulep_dec = ULEP(latent_dim=args.latent_dim, use_pretrained=use_pretrained)
    mrgwd = MRGWD(latent_dim=args.latent_dim, output_size=output_size, use_vae=False)

    ulep_enc.eval()
    ulep_dec.eval()
    mrgwd.eval()
    print("  Models ready.")

    # Build encoder/decoder
    encoder = OmniQuantEncoder(
        ulep=ulep_enc,
        latent_dim=args.latent_dim,
        keyframe_interval=args.keyframe_interval,
        lcc_threshold=args.lcc_threshold,
        sparse_fraction=args.sparse_fraction,
        gtm_bits_keyframe=args.gtm_bits_kf,
        gtm_bits_predictive=args.gtm_bits_pf,
    )
    decoder = OmniQuantDecoder(
        ulep=ulep_dec,
        mrgwd=mrgwd,
        latent_dim=args.latent_dim,
        sparse_fraction=args.sparse_fraction,
    )

    # Encode + decode all frames
    print(f"\n[3/4] Encoding & decoding {len(frames)} frames...")
    metrics = MetricsAccumulator(fps=30.0)
    decoded_frames = []
    lats = []

    for i, pil_frame in enumerate(tqdm(frames, desc="Encoding")):
        t0 = time.perf_counter()
        packet_bytes, enc_stats = encoder.encode_frame(pil_frame)
        frame_tensor, dec_stats = decoder.decode_packet(packet_bytes)
        dt = time.perf_counter() - t0

        original_t = pil_to_tensor(pil_frame)
        metrics.update(original_t, frame_tensor.cpu(),
                       enc_stats.packet_bytes,
                       is_keyframe=enc_stats.is_keyframe,
                       lcc_triggered=enc_stats.lcc_triggered)
        decoded_frames.append(tensor_to_pil(frame_tensor.cpu(), size=output_size))
        lats.append(dt * 1000)

        if i % 10 == 0:
            summary = metrics.summary()
            print(f"  Frame {i:4d} | {'KF' if enc_stats.is_keyframe else 'PF'} | "
                  f"{enc_stats.packet_bytes:5d} bytes | "
                  f"PSNR={metrics.psnr_values[-1]:.1f}dB | "
                  f"Bitrate={metrics.instantaneous_bitrate_mbps:.3f} Mbps | "
                  f"Lat={dt*1000:.1f}ms")

    # Results
    print("\n[4/4] Results:")
    print("=" * 60)
    s = metrics.summary()
    for k, v in s.items():
        print(f"  {k:<25} {v}")
    print(f"  {'avg_latency_ms':<25} {sum(lats)/len(lats):.1f}")
    print("=" * 60)

    # Save comparison video
    if args.output_video or args.mode == "headless":
        out_path = args.output_video or "demo_output.gif"
        print(f"\nSaving comparison GIF to: {out_path}")
        comparison = []
        for orig, dec in zip(frames[:30], decoded_frames[:30]):
            # Side by side
            orig_r = orig.resize(output_size, Image.LANCZOS)
            combined = Image.new("RGB", (output_size[0] * 2, output_size[1]))
            combined.paste(orig_r, (0, 0))
            combined.paste(dec, (output_size[0], 0))
            comparison.append(combined)
        comparison[0].save(
            out_path, save_all=True, append_images=comparison[1:],
            duration=67, loop=0
        )
        print(f"Saved {len(comparison)}-frame comparison GIF.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
