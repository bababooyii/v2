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
if torch.cuda.is_available():
    print(f"GPU: {torch.cuda.get_device_name()}")

from ulep.model import ULEP
from mrgwd.model import MRGWD
from gtm.codec import GTMEncoder, GTMDecoder
from demo.metrics import compute_psnr

# ============================================================
# CONFIG
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = 256  # VAE expects 256
OUTPUT_SIZE = 256
BATCH_SIZE = 2
NUM_VIDEOS = 150
FRAMES_PER_VIDEO = 8
NUM_EPOCHS = 20
LR = 2e-5
GRAD_CLIP = 0.3
CHECKPOINT_PATH = "/kaggle/working/omniquant_apex_v5.pt"

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

ulep = ULEP(latent_dim=512, use_pretrained=True).cuda()
mrgwd = MRGWD(latent_dim=512, output_size=(OUTPUT_SIZE, OUTPUT_SIZE), use_vae=True, force_vae=True).cuda()

gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
gtm_dec = GTMDecoder(qjl_proj_dim=64)

# Freeze
for p in ulep.backbone.parameters():
    p.requires_grad = False
for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

# Trainable: encode_head + projector
trainable = list(ulep.encode_head.parameters()) + list(mrgwd.latent_synth.projector.parameters())
print(f"Trainable params: {sum(p.numel() for p in trainable):,}")

optimizer = torch.optim.AdamW(trainable, lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.OneCycleLR(
    optimizer, max_lr=LR, epochs=NUM_EPOCHS, steps_per_epoch=300
)

# ============================================================
# DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Total videos: {len(video_files)}")
train_videos = video_files[:NUM_VIDEOS]
test_videos = video_files[NUM_VIDEOS:NUM_VIDEOS + 10]

# ============================================================
# PREPROCESSING
# ============================================================
def preprocess_vae(img):
    """Preprocess for VAE (256x256)"""
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.LANCZOS)
    arr = np.array(img).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

def preprocess_dino(img):
    """Preprocess for DINO (224x224, normalized)"""
    img = img.resize((224, 224), Image.LANCZOS)
    arr = np.array(img).astype(np.float32) / 255.0
    arr = (arr - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# DISTILLATION: Train projector to match VAE encoder output
# ============================================================
print(f"\n🚀 Distillation training {NUM_EPOCHS} epochs...")

ulep.train()
mrgwd.train()

criterion = nn.MSELoss()
vae_scale = mrgwd.latent_synth._scale_factor
scaler = torch.amp.GradScaler('cuda')
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
                
                # Preprocess for VAE and DINO
                x_vae = torch.stack([preprocess_vae(f) for f in batch_frames]).cuda().half()  # VAE is fp16
                x_dino = torch.stack([preprocess_dino(f) for f in batch_frames]).cuda()
                
                with torch.no_grad():
                    # Get VAE latent as "ground truth"
                    vae_latent = mrgwd.latent_synth.vae.encode(x_vae).latent_dist.sample()
                    vae_latent = vae_latent * vae_scale
                    vae_latent_flat = vae_latent.flatten(1)
                    
                    # Get DINO features
                    dino_feat = ulep.backbone(x_dino)
                
                # Project DINO to match VAE latent
                dino_latent = ulep.encode_head(dino_feat)
                dino_proj = mrgwd.latent_synth.projector(dino_latent)
                dino_proj_flat = dino_proj.flatten(1)
                
                # Match VAE latent
                loss = criterion(dino_proj_flat, vae_latent_flat.detach())
                
                optimizer.zero_grad()
                scaler.scale(loss).backward()
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(trainable, max_norm=GRAD_CLIP)
                scaler.step(optimizer)
                scaler.update()
                scheduler.step()
                
                epoch_loss += loss.item()
                num_batches += 1
                
        except Exception as e:
            print(f"Error: {e}")
            continue
        
        if (vi + 1) % 30 == 0:
            print(f"  Video {vi+1}/{len(train_videos)}, loss: {epoch_loss/max(num_batches,1):.4f}")
    
    avg_loss = epoch_loss / max(num_batches, 1)
    elapsed = time.time() - epoch_start
    lr_now = optimizer.param_groups[0]['lr']
    
    print(f"Epoch {epoch+1:2d}/{NUM_EPOCHS} | Loss: {avg_loss:.4f} | LR: {lr_now:.6f} | Time: {elapsed:.1f}s")
    
    if avg_loss < best_loss:
        best_loss = avg_loss
        torch.save({
            "epoch": epoch,
            "ulep_encode_head": ulep.encode_head.state_dict(),
            "mrgwd_projector": mrgwd.latent_synth.projector.state_dict(),
            "optimizer": optimizer.state_dict(),
            "loss": avg_loss,
        }, CHECKPOINT_PATH)
        print(f"  💾 Saved")

print("\n✅ Distillation complete!")

# ============================================================
# EVALUATION
# ============================================================
print("\n🧪 Evaluating reconstruction...")

ulep.eval()
mrgwd.eval()

psnr_vals = []

for video in test_videos[:5]:
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
            x_vae = preprocess_vae(img).unsqueeze(0).cuda().half()
            x_dino = preprocess_dino(img).unsqueeze(0).cuda()
            
            with torch.no_grad():
                # Ground truth
                vae_latent = mrgwd.latent_synth.vae.encode(x_vae).latent_dist.sample()
                
                # Predicted
                dino_feat = ulep.backbone(x_dino)
                dino_latent = ulep.encode_head(dino_feat)
                dino_proj = mrgwd.latent_synth.projector(dino_latent)
                
                # Decode both - reshape projector output back to (B, 4, 32, 32)
                dino_vae_latent = dino_proj.view(dino_proj.shape[0], 4, 32, 32)
                target = mrgwd.latent_synth.vae.decode(vae_latent / vae_scale).sample
                pred = mrgwd.latent_synth.vae.decode(dino_vae_latent / vae_scale).sample
                
                psnr = compute_psnr(target.squeeze(0).cpu(), pred.squeeze(0).cpu())
                psnr_vals.append(psnr)
                
    except Exception as e:
        print(f"Eval error: {e}")
        continue

if psnr_vals:
    print(f"\n📊 TEST PSNR (distilled vs GT): {np.mean(psnr_vals):.2f} dB")
else:
    print("No PSNR computed")

print(f"\n💾 Checkpoint: {CHECKPOINT_PATH}")
