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
# CONFIG - Direct image reconstruction
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = 224  # DINO input
OUTPUT_SIZE = 256
BATCH_SIZE = 2
NUM_VIDEOS = 200  # More videos
FRAMES_PER_VIDEO = 4
NUM_EPOCHS = 25
LR = 1e-4
GRAD_CLIP = 0.5
CHECKPOINT_PATH = "/kaggle/working/omniquant_apex_v6.pt"

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

ulep = ULEP(latent_dim=512, use_pretrained=True).cuda()
mrgwd = MRGWD(latent_dim=512, output_size=(OUTPUT_SIZE, OUTPUT_SIZE), use_vae=True, force_vae=True).cuda()

# Freeze backbone and VAE
for p in ulep.backbone.parameters():
    p.requires_grad = False
for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

# Train encode_head + projector
trainable = list(ulep.encode_head.parameters()) + list(mrgwd.latent_synth.projector.parameters())
print(f"Trainable params: {sum(p.numel() for p in trainable):,}")

optimizer = torch.optim.AdamW(trainable, lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=NUM_EPOCHS)

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
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
    arr = np.array(img).astype(np.float32) / 255.0
    arr = (arr - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    return torch.from_numpy(arr).permute(2, 0, 1).float()

def target_tensor(img):
    arr = np.array(img.resize((OUTPUT_SIZE, OUTPUT_SIZE))).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# TRAINING - Direct reconstruction
# ============================================================
print(f"\n🚀 Direct reconstruction training {NUM_EPOCHS} epochs...")

ulep.train()
mrgwd.train()

criterion = nn.MSELoss()
best_loss = float('inf')

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
                
                # Inputs
                x = torch.stack([preprocess(f) for f in batch_frames]).cuda()
                targets = torch.stack([target_tensor(f) for f in batch_frames]).cuda()
                
                # Forward: DINO -> encode_head -> projector -> VAE decode
                with torch.no_grad():
                    features = ulep.backbone(x)
                
                z = ulep.encode_head(features)
                z_norm = F.normalize(z, dim=-1)
                
                # Project to VAE latent space
                vae_latent = mrgwd.latent_synth.projector(z_norm)
                vae_latent_4d = vae_latent.view(vae_latent.shape[0], 4, 32, 32)
                
                # Decode with VAE (no gradients through VAE)
                with torch.no_grad():
                    decoded = mrgwd.latent_synth.vae.decode(vae_latent_4d / mrgwd.latent_synth._scale_factor).sample
                
                if decoded.shape != targets.shape:
                    decoded = F.interpolate(decoded, size=targets.shape[-2:], mode='bilinear', align_corners=False)
                
                loss = criterion(decoded, targets)
                
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
            "ulep_encode_head": ulep.encode_head.state_dict(),
            "mrgwd_projector": mrgwd.latent_synth.projector.state_dict(),
            "loss": avg_loss,
        }, CHECKPOINT_PATH)
        print(f"  💾 Saved")

print("\n✅ Training complete!")

# ============================================================
# EVALUATION
# ============================================================
print("\n🧪 Evaluating...")

ulep.eval()
mrgwd.eval()

gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
gtm_dec = GTMDecoder(qjl_proj_dim=64)

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
        
        for img in frames[:2]:
            x = preprocess(img).unsqueeze(0).cuda()
            target = target_tensor(img)
            
            with torch.no_grad():
                features = ulep.backbone(x)
                z = ulep.encode_head(features).squeeze(0)
                z_norm = F.normalize(z, dim=-1)
                
                # With GTM compression
                packets = gtm_enc.encode(z_norm.cpu())
                z_dec = gtm_dec.decode(packets, 512).cuda()
                
                vae_latent = mrgwd.latent_synth.projector(z_dec.unsqueeze(0))
                vae_latent_4d = vae_latent.view(1, 4, 32, 32)
                dec = mrgwd.latent_synth.vae.decode(vae_latent_4d / mrgwd.latent_synth._scale_factor).sample
                
                if dec.shape != target.shape:
                    dec = F.interpolate(dec, size=target.shape[-2:], mode='bilinear', align_corners=False)
                
                psnr = compute_psnr(target, dec.squeeze(0).cpu())
                psnr_vals.append(psnr)
                
    except Exception as e:
        print(f"Eval error: {e}")
        continue

if psnr_vals:
    print(f"\n📊 TEST PSNR (with GTM): {np.mean(psnr_vals):.2f} dB (std: {np.std(psnr_vals):.2f})")
else:
    print("No PSNR computed")

print(f"\n💾 Checkpoint: {CHECKPOINT_PATH}")