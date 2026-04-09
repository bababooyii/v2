#!/usr/bin/env python3
"""
OmniQuant-Apex: CLIP-Based Encoder (Option 2)
Zero training required - uses frozen CLIP embeddings from transformers
Much better than random weights!
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch
import numpy as np
from PIL import Image
import time


class CLIPEncoder:
    """
    Zero-training encoder using frozen CLIP ViT-L/14 embeddings.
    Uses timm to avoid torchvision compatibility issues.
    """
    
    def __init__(self, latent_dim: int = 512):
        self.latent_dim = latent_dim
        print("[CLIPEncoder] Loading CLIP ViT-L/14 from timm...")
        import timm
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.model = timm.create_model('vit_large_patch14_clip_224', pretrained=True, num_classes=0)
        self.model.eval()
        
        # Get feature dim
        self.feat_dim = self.model.num_features  # 768 for CLIP ViT-L
        
        # Project to latent_dim
        self.projector = torch.nn.Linear(self.feat_dim, latent_dim)
        torch.nn.init.xavier_uniform_(self.projector.weight)
        torch.nn.init.zeros_(self.projector.bias)
        
        # Preprocessing
        self.mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        self.std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        
    def _preprocess(self, img: Image.Image) -> torch.Tensor:
        """Preprocess image for CLIP."""
        img = img.resize((224, 224), Image.LANCZOS)
        arr = np.array(img, dtype=np.float32) / 255.0
        arr = (arr - self.mean) / self.std
        return torch.from_numpy(arr.transpose(2, 0, 1)).float()
        
    def encode(self, frame) -> torch.Tensor:
        """Encode frame to latent z_t using CLIP."""
        if isinstance(frame, Image.Image):
            x = self._preprocess(frame).unsqueeze(0)
        else:
            x = frame
            
        with torch.no_grad():
            features = self.model(x)  # (1, 768)
            features = features / features.norm(dim=-1, keepdim=True)
            z = self.projector(features)
            z = z / z.norm(dim=-1, keepdim=True)
        return z.squeeze(0)


class CLIPPredictor:
    """
    Simple linear extrapolation predictor.
    Uses velocity-based prediction: z_hat_t = z_{t-1} + (z_{t-1} - z_{t-2})
    """
    
    def __init__(self):
        self.z_prev: torch.Tensor = None
        self.z_prev2: torch.Tensor = None
        
    def predict(self) -> torch.Tensor:
        """Predict next latent using linear extrapolation."""
        if self.z_prev is None:
            return self.z_prev  # Repeat last
        
        if self.z_prev2 is None:
            return self.z_prev  # Not enough history
            
        # Linear extrapolation
        velocity = self.z_prev - self.z_prev2
        z_hat = self.z_prev + velocity
        return z_hat / z_hat.norm(dim=-1, keepdim=True)
    
    def update(self, z: torch.Tensor):
        self.z_prev2 = self.z_prev
        self.z_prev = z.detach()
    
    def reset(self):
        self.z_prev = None
        self.z_prev2 = None


class CLIPULEP:
    """Wrapper combining CLIP encoder + predictor for codec compatibility."""
    
    def __init__(self, latent_dim: int = 512):
        self.encoder = CLIPEncoder(latent_dim)
        self.predictor = CLIPPredictor()
        self.latent_dim = latent_dim
        self._z_t_minus_1: torch.Tensor = None
        self._z_t_minus_2: torch.Tensor = None
        
    def encode(self, frame) -> torch.Tensor:
        return self.encoder.encode(frame)
        
    def predict(self) -> torch.Tensor:
        return self.predictor.predict()
    
    def update_state(self, z_t: torch.Tensor):
        self._z_t_minus_2 = self._z_t_minus_1
        self._z_t_minus_1 = z_t.detach()
        self.predictor.update(z_t)
        
    def reset_state(self):
        self._z_t_minus_1 = None
        self._z_t_minus_2 = None
        self.predictor.reset()


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
    print("  OmniQuant-Apex: CLIP-Based Encoder (Option 2)")
    print("  Zero training - using frozen CLIP ViT-L/14")
    print("=" * 60)
    print()
    
    LATENT_DIM = 512
    OUTPUT_SIZE = (256, 256)
    N_FRAMES = 30
    
    # Build models
    print("[1/3] Loading CLIP encoder (frozen)...")
    ulep_enc = CLIPULEP(latent_dim=LATENT_DIM)
    ulep_dec = CLIPULEP(latent_dim=LATENT_DIM)
    mrgwd = MRGWD(latent_dim=LATENT_DIM, output_size=OUTPUT_SIZE, use_vae=False, force_vae=False)
    print("  CLIP encoder ready!")
    print()
    
    # Build encoder/decoder (need to wrap CLIPULEP to work with codec)
    print("[2/3] Building codec pipeline...")
    
    # Create a minimal encoder wrapper
    class ClipEncoderWrapper:
        """Wrapper to make CLIPULEP work with OmniQuantEncoder."""
        def __init__(self, clip_ulep):
            self.ulep = clip_ulep
            self.latent_dim = clip_ulep.latent_dim
            
        def encode(self, frame):
            return self.ulep.encode(frame)
            
        def predict(self):
            return self.ulep.predict()
            
        def update_state(self, z):
            self.ulep.update_state(z)
            
        def reset_state(self):
            self.ulep.reset_state()
            
        def set_lcc_threshold(self, t):
            pass
            
        def set_sparse_fraction(self, f):
            pass
    
    # We need a minimal encoder class that works with the codec
    # Let's create a simpler approach - modify encoder to accept our wrapper
    
    # Actually, let's just modify the codec call to use our custom encode method
    # For now, let's test the CLIP encoder directly
    
    encoder = ClipEncoderWrapper(ulep_enc)
    decoder = ClipEncoderWrapper(ulep_dec)
    
    print("  Codec ready!")
    print()
    
    # Run demo - using custom encoding loop to work with CLIP wrapper
    print(f"[3/3] Running {N_FRAMES}-frame demo...")
    print("-" * 60)
    
    psnr_vals = []
    ssim_vals = []
    bitrates = []
    keyframes = 0
    
    # GTM for quantization
    from gtm.codec import GTMEncoder, GTMDecoder
    
    gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
    gtm_dec = GTMDecoder(qjl_proj_dim=64)
    
    encoder.reset_state()
    decoder.reset_state()
    
    start_time = time.perf_counter()
    
    for i in range(N_FRAMES):
        # Generate frame
        orig_img = generate_test_frame(i, OUTPUT_SIZE)
        orig_tensor = pil_to_tensor(orig_img)
        
        t0 = time.perf_counter()
        
        # Encode using CLIP
        z_t = encoder.encode(orig_img)
        
        # Keyframe decision (simple: every 30 frames)
        is_keyframe = (i % 30 == 0)
        
        if is_keyframe:
            # Full encoding
            gtm_packets = gtm_enc.encode(z_t)
            from codec.packets import KeyframePacket
            pkt = KeyframePacket(
                frame_idx=i,
                latent_dim=LATENT_DIM,
                gtm_packets=gtm_packets,
            )
            keyframes += 1
        else:
            # Predictive encoding
            z_hat = encoder.predict()
            if z_hat is not None:
                delta_z = z_t - z_hat
                # Simple sparse: top-k
                k = max(8, int(LATENT_DIM * 0.25))
                magnitudes = delta_z.abs()
                top_k = torch.topk(magnitudes, k=k, largest=True)
                indices = top_k.indices
                values = delta_z[indices]
                
                # Quantize sparse values
                gtm_packets = gtm_enc.encode(values)
                from codec.packets import PredictivePacket
                pkt = PredictivePacket(
                    frame_idx=i,
                    latent_dim=LATENT_DIM,
                    top_k=k,
                    indices=indices.tolist(),
                    gtm_packets=gtm_packets,
                )
            else:
                # Not enough history, make keyframe
                gtm_packets = gtm_enc.encode(z_t)
                from codec.packets import KeyframePacket
                pkt = KeyframePacket(
                    frame_idx=i,
                    latent_dim=LATENT_DIM,
                    gtm_packets=gtm_packets,
                )
                keyframes += 1
        
        # Serialize
        from codec.packets import serialize_packet
        packet_bytes = serialize_packet(pkt)
        
        # Update encoder state
        encoder.update_state(z_t)
        
        # Decode
        from codec.packets import deserialize_packet
        pkt_dec = deserialize_packet(packet_bytes)
        
        # Reconstruct
        if isinstance(pkt_dec, KeyframePacket):
            z_dec = gtm_dec.decode(pkt_dec.gtm_packets, LATENT_DIM)
        else:
            z_hat_dec = decoder.predict() if decoder.predict() is not None else torch.zeros(LATENT_DIM)
            quant_values = gtm_dec.decode(pkt_dec.gtm_packets, pkt_dec.top_k)
            # Reconstruct sparse
            delta_z_dec = torch.zeros(LATENT_DIM)
            delta_z_dec[pkt_dec.indices] = quant_values
            z_dec = z_hat_dec + delta_z_dec
        
        # Decode to image
        dec_tensor = mrgwd.synthesize(z_dec)
        
        dt = (time.perf_counter() - t0) * 1000
        
        # Update decoder state
        decoder.update_state(z_dec)
        
        # Metrics
        psnr = compute_psnr(orig_tensor, dec_tensor)
        ssim = compute_ssim(orig_tensor, dec_tensor)
        bitrate_mbps = (len(packet_bytes) * 8) / (1/30 * 1e6)
        
        psnr_vals.append(psnr)
        ssim_vals.append(ssim)
        bitrates.append(bitrate_mbps)
        
        print(f"  Frame {i:2d} | {'KF' if is_keyframe else 'PF'} | "
              f"{len(packet_bytes):4d}B | PSNR={psnr:.1f}dB | "
              f"SSIM={ssim:.3f} | {bitrate_mbps:.3f}Mbps | {dt:.0f}ms")
    
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
    print(f"  Keyframes:         {keyframes}")
    print()
    print("NOTE: CLIP provides much better embeddings than random weights.")
    print("Quality is still limited by:")
    print("  - ConvFallbackDecoder (random weights, no SD-VAE on CPU)")
    print("  - Simple linear predictor")
    print()
    print("Next steps to improve:")
    print("  1. Add GPU for SD-VAE (much better images)")
    print("  2. Fine-tune just the projector on small local dataset")
    print("  3. Use optical flow for better temporal prediction")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())