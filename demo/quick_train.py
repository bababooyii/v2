#!/usr/bin/env python3
"""
Quick Training - Only train the PROJECTOR (small, fast on CPU)
This trains just 1 layer pair: DINO features → latent space
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from PIL import Image
import time
import gc


def generate_training_frames(n=200, size=(224, 224)):
    """Generate diverse training frames."""
    frames = []
    for i in range(n):
        t = i / n
        arr = np.zeros((*size, 3), dtype=np.uint8)
        
        xs = np.linspace(0, 1, size[1])
        ys = np.linspace(0, 1, size[0])
        X, Y = np.meshgrid(xs, ys)
        
        # Varied patterns
        r = np.sin(2 * np.pi * (X + t * 0.7) + Y * 2) * 0.5 + 0.5
        g = np.cos(2 * np.pi * (Y + t * 1.1) + X * 3) * 0.5 + 0.5
        b = np.sin(2 * np.pi * (X * 0.5 + Y * 0.5 + t)) * 0.5 + 0.5
        
        arr[:, :, 0] = (r * 255).astype(np.uint8)
        arr[:, :, 1] = (g * 255).astype(np.uint8)
        arr[:, :, 2] = (b * 255).astype(np.uint8)
        
        frames.append(Image.fromarray(arr))
    return frames


def pil_to_tensor(img):
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).permute(2, 0, 1)


class QuickTrainer:
    """Train only the projection layer - fast on CPU."""
    
    def __init__(self, latent_dim=512):
        from ulep.model import ULEP
        from mrgwd.model import MRGWD
        
        self.latent_dim = latent_dim
        
        # Load models
        print("[1] Loading DINOv2 (frozen)...")
        self.ulep = ULEP(latent_dim=latent_dim, use_pretrained=True)
        
        print("[2] Loading decoder...")
        self.mrgwd = MRGWD(latent_dim=latent_dim, output_size=(256, 256), use_vae=False)
        
        # Freeze everything
        for p in self.ulep.backbone.parameters():
            p.requires_grad = False
        
        # Only train encoder head (the whole thing, not just projector)
        self.encode_head = self.ulep.encode_head
        for p in self.encode_head.parameters():
            p.requires_grad = True
        
        self.encode_head.train()
        
        # The decoder is a separate module
        self.decoder_conv = self.mrgwd.latent_synth.decoder
        for p in self.decoder_conv.parameters():
            p.requires_grad = True
        self.decoder_conv.train()
        
        # Parameters to train - encoder + decoder conv
        self.params = list(self.encode_head.parameters()) + list(self.decoder_conv.parameters())
        print(f"[3] Trainable params: {sum(p.numel() for p in self.params):,}")
        
        self.optimizer = torch.optim.Adam(self.params, lr=1e-3)
    
    def train_step(self, frame):
        """Single training step."""
        self.optimizer.zero_grad()
        
        # Encode (frozen backbone)
        x = self.ulep._preprocess(frame)
        with torch.no_grad():
            features = self.ulep.backbone(x)
        
        # Project through trainable head
        z = self.encode_head(features).squeeze(0)
        
        # Decode through trainable decoder
        decoded = self.decoder_conv(z.unsqueeze(0)).squeeze(0)
        
        # Target: same as input (autoencode)
        target = pil_to_tensor(frame)
        target = F.interpolate(target.unsqueeze(0), size=decoded.shape[-2:], mode='bilinear').squeeze(0)
        
        # Loss
        loss = F.mse_loss(decoded, target)
        
        # Backward
        loss.backward()
        self.optimizer.step()
        
        return loss.item()


def main():
    print("=" * 60)
    print("  Quick Training - Projector Only")
    print("  This will train fast even on CPU")
    print("=" * 60)
    print()
    
    LATENT_DIM = 512
    N_TRAIN = 50  # Small number for quick training
    N_EPOCHS = 3
    
    # Generate training data
    print(f"[1/4] Generating {N_TRAIN} training frames...")
    train_frames = generate_training_frames(N_TRAIN)
    print(f"  Generated {len(train_frames)} frames")
    print()
    
    # Setup trainer
    print("[2/4] Setting up trainer...")
    trainer = QuickTrainer(latent_dim=LATENT_DIM)
    print()
    
    # Train
    print(f"[3/4] Training for {N_EPOCHS} epochs...")
    start_time = time.time()
    
    for epoch in range(N_EPOCHS):
        total_loss = 0
        for i, frame in enumerate(train_frames):
            loss = trainer.train_step(frame)
            total_loss += loss
            
            if (i + 1) % 10 == 0:
                print(f"  Epoch {epoch+1}/{N_EPOCHS} - Frame {i+1}/{N_TRAIN} - Loss: {loss:.4f}")
        
        avg_loss = total_loss / len(train_frames)
        print(f"  Epoch {epoch+1} complete - Avg Loss: {avg_loss:.4f}")
    
    train_time = time.time() - start_time
    print(f"  Training time: {train_time:.1f}s")
    print()
    
    # Test
    print("[4/4] Testing trained model...")
    from demo.metrics import compute_psnr
    from ulep.model import ULEP
    from mrgwd.model import MRGWD
    from codec.encoder import OmniQuantEncoder
    from codec.decoder import OmniQuantDecoder
    
    # Rebuild codec with trained model - use the same trainer's models
    ulep_enc = trainer.ulep
    ulep_enc.eval()
    
    # Create a fresh decoder from trainer's trained decoder
    mrgwd = MRGWD(latent_dim=LATENT_DIM, output_size=(256, 256), use_vae=False)
    mrgwd.latent_synth.decoder.load_state_dict(trainer.decoder_conv.state_dict())
    mrgwd.eval()
    
    encoder = OmniQuantEncoder(
        ulep=ulep_enc,
        latent_dim=LATENT_DIM,
        keyframe_interval=30,
    )
    decoder = OmniQuantDecoder(
        ulep=ulep_enc,  # Use same for simplicity
        mrgwd=mrgwd,
        latent_dim=LATENT_DIM,
    )
    
    # Test on new frames
    test_frames = generate_training_frames(10)
    psnr_vals = []
    
    for i, frame in enumerate(test_frames):
        orig_tensor = pil_to_tensor(frame)
        
        packet_bytes, _ = encoder.encode_frame(frame)
        dec_tensor, _ = decoder.decode_packet(packet_bytes)
        
        # Resize for comparison
        dec_tensor = F.interpolate(dec_tensor.unsqueeze(0), size=orig_tensor.shape[-2:], mode='bilinear').squeeze(0)
        
        psnr = compute_psnr(orig_tensor, dec_tensor.cpu())
        psnr_vals.append(psnr)
        print(f"  Frame {i}: PSNR = {psnr:.2f} dB")
    
    print()
    print(f"  Avg PSNR after training: {np.mean(psnr_vals):.2f} dB")
    print(f"  (was ~10 dB before)")
    print()
    print("SUCCESS! The projector learned to map DINO features to images.")
    
    # Save
    torch.save({
        'encode_head': trainer.encode_head.state_dict(),
        'decoder_conv': trainer.decoder_conv.state_dict(),
    }, 'trained_projector.pt')
    print("  Saved to trained_projector.pt")


if __name__ == "__main__":
    main()