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

from ulep.model import ULEP
from mrgwd.model import MRGWD
from gtm.codec import GTMEncoder, GTMDecoder
from demo.metrics import compute_psnr

# ============================================================
# CONFIG - VAE encoder + learnable bottleneck
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = 256
OUTPUT_SIZE = 256
BATCH_SIZE = 2
NUM_VIDEOS = 200
FRAMES_PER_VIDEO = 4
NUM_EPOCHS = 30
LR = 3e-4
GRAD_CLIP = 1.0
CHECKPOINT_PATH = "/kaggle/working/omniquant_apex_v7.pt"

# ============================================================
# LEARNABLE BOTTLENECK - Compresses VAE latent to 512 dim
# ============================================================
class LearnableBottleneck(nn.Module):
    """
    Compresses VAE latent (4,32,32)=4096 dims to 512 dims for GTM
    Then expands back for decoding.
    """
    def __init__(self, vae_dim=4096, bottleneck_dim=512):
        super().__init__()
        self.vae_dim = vae_dim
        self.bottleneck_dim = bottleneck_dim
        
        # Compress: 4096 -> 512
        self.compress = nn.Sequential(
            nn.Linear(vae_dim, 1024),
            nn.GELU(),
            nn.Linear(1024, bottleneck_dim),
        )
        
        # Expand: 512 -> 4096
        self.expand = nn.Sequential(
            nn.Linear(bottleneck_dim, 1024),
            nn.GELU(),
            nn.Linear(1024, vae_dim),
        )
    
    def forward(self, x):
        # x: (B, 4, 32, 32) = (B, 4096)
        flat = x.flatten(1)
        compressed = self.compress(flat)
        expanded = self.expand(compressed)
        return expanded.view(x.shape[0], 4, 32, 32)

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

ulep = ULEP(latent_dim=512, use_pretrained=True).cuda()
mrgwd = MRGWD(latent_dim=512, output_size=(OUTPUT_SIZE, OUTPUT_SIZE), use_vae=True, force_vae=True).cuda()

# Add learnable bottleneck
bottleneck = LearnableBottleneck(vae_dim=4096, bottleneck_dim=512).cuda()
print(f"Bottleneck params: {sum(p.numel() for p in bottleneck.parameters()):,}")

# Freeze backbone and VAE decoder (keep encoder for extracting latents)
for p in ulep.backbone.parameters():
    p.requires_grad = False
for p in mrgwd.latent_synth.vae.encoder.parameters():
    p.requires_grad = False  # We'll use it but not train
for p in mrgwd.latent_synth.vae.decoder.parameters():
    p.requires_grad = False

# Train only the bottleneck
trainable = list(bottleneck.parameters())
print(f"Trainable params: {sum(p.numel() for p in trainable):,}")

optimizer = torch.optim.AdamW(trainable, lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=NUM_EPOCHS)

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
test_videos = video_files[NUM_VIDEOS:NUM_VIDEOS + 15]

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess(img):
    arr = np.array(img.resize((IMAGE_SIZE, IMAGE_SIZE))).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# TRAINING - VAE encoder -> bottleneck -> GTM -> expand -> VAE decode
# ============================================================
print(f"\n🚀 VAE bottleneck training {NUM_EPOCHS} epochs...")

bottleneck.train()
mrgwd.latent_synth.vae.eval()

criterion = nn.MSELoss()
best_loss = float('inf')
vae_scale = mrgwd.latent_synth._scale_factor

for epoch in range(NUM_EPOCHS):
    epoch_start = time.time()
    epoch_loss = 0
    num_batches = 0
    
    np.random.shuffle(train_videos)
    
    for vi, video_path in enumerate(train_videos):
        try:
            cap = cv2.VideoCapture(video_path)
            frames = []
            while len(frames) < FRAMES_PER_VIDEO:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frames.append(Image.fromarray(frame))
            cap.release()
            
            if len(frames) < 2:
                continue
            
            for i in range(0, len(frames) - 1, BATCH_SIZE):
                batch_frames = frames[i:i + BATCH_SIZE]
                if len(batch_frames) < 2:
                    break
                
                x = torch.stack([preprocess(f) for f in batch_frames]).cuda()
                
                # Get VAE latent (full pixel info!)
                with torch.no_grad():
                    vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                    vae_latent_scaled = vae_latent * vae_scale
                
                # Compress through bottleneck
                compressed = bottleneck(vae_latent_scaled)
                
                # GTM encode/decode - compress to bits
                # Process each sample individually
                decoded_list = []
                for j in range(compressed.shape[0]):
                    z_single = compressed[j]  # (4, 32, 32)
                    z_flat = z_single.flatten()  # 4096
                    
                    # Normalize for GTM
                    z_norm = z_flat / (z_flat.norm() + 1e-8)
                    
                    # Encode single sample
                    packets = gtm_enc.encode(z_norm.unsqueeze(0).cpu())
                    z_dec = gtm_dec.decode(packets, 4096).cuda()
                    
                    # Decode single sample
                    dec = mrgwd.latent_synth.vae.decode(z_dec.view(1, 4, 32, 32) / vae_scale).sample
                    decoded_list.append(dec)
                
                decoded = torch.cat(decoded_list, dim=0)
                
                # Loss: reconstruction quality
                loss = criterion(decoded, x)
                
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(trainable, max_norm=GRAD_CLIP)
                optimizer.step()
                
                epoch_loss += loss.item()
                num_batches += 1
                
        except Exception as e:
            print(f"Error: {e}")
            continue
        
        if (vi + 1) % 40 == 0:
            print(f"  Video {vi+1}/{len(train_videos)}, loss: {epoch_loss/max(num_batches,1):.4f}")
    
    scheduler.step()
    avg_loss = epoch_loss / max(num_batches, 1)
    elapsed = time.time() - epoch_start
    
    print(f"Epoch {epoch+1:2d}/{NUM_EPOCHS} | Loss: {avg_loss:.4f} | LR: {scheduler.get_last_lr()[0]:.6f} | Time: {elapsed:.1f}s")
    
    if avg_loss < best_loss:
        best_loss = avg_loss
        torch.save({
            "epoch": epoch,
            "bottleneck": bottleneck.state_dict(),
            "loss": avg_loss,
        }, CHECKPOINT_PATH)
        print(f"  💾 Saved")

print("\n✅ Training complete!")

# ============================================================
# EVALUATION
# ============================================================
print("\n🧪 Evaluating...")

bottleneck.eval()
mrgwd.latent_synth.vae.eval()

psnr_vals = []

for video in test_videos[:8]:
    try:
        cap = cv2.VideoCapture(video)
        frames = []
        while len(frames) < 5:
            ret, frame = cap.read()
            if not ret:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(frame))
        cap.release()
        
        for img in frames[:3]:
            x = preprocess(img).unsqueeze(0).cuda()
            target = x.squeeze(0)
            
            with torch.no_grad():
                # Encode
                vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
                vae_latent_scaled = vae_latent * vae_scale
                
                # Compress
                compressed = bottleneck(vae_latent_scaled)
                
                # GTM encode/decode
                z_flat = compressed.squeeze(0).flatten()
                z_norm = z_flat / (z_flat.norm() + 1e-8)
                
                packets = gtm_enc.encode(z_norm.unsqueeze(0).cpu())
                z_dec = gtm_dec.decode(packets, 4096).cuda()
                
                # Decode
                dec = mrgwd.latent_synth.vae.decode(z_dec.view(1, 4, 32, 32) / vae_scale).sample
                
                psnr = compute_psnr(target, dec.squeeze(0).cpu())
                psnr_vals.append(psnr)
                
    except Exception as e:
        print(f"Eval error: {e}")
        continue

if psnr_vals:
    print(f"\n📊 TEST PSNR: {np.mean(psnr_vals):.2f} dB (std: {np.std(psnr_vals):.2f})")
else:
    print("No PSNR computed")

print(f"\n💾 Checkpoint: {CHECKPOINT_PATH}")