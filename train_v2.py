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
# CONFIGURATION
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
IMAGE_SIZE = 224  # DINOv2 input size
BATCH_SIZE = 4
NUM_EPOCHS = 20
LR = 1e-4
GRAD_CLIP = 1.0
TRAINABLE = ["encode_head", "projector"]  # What to train

# ============================================================
# EXTRACT FRAMES FROM VIDEOS
# ============================================================
def extract_frames(video_path, max_frames=16):
    frames = []
    try:
        cap = cv2.VideoCapture(video_path)
        count = 0
        while count < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(frame))
            count += 1
        cap.release()
    except Exception as e:
        print(f"Error reading {video_path}: {e}")
    return frames

def preprocess_image(img, target_size=IMAGE_SIZE):
    img = img.resize((target_size, target_size), Image.LANCZOS)
    arr = np.array(img).astype(np.float32) / 255.0
    arr = (arr - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

ulep = ULEP(latent_dim=512, use_pretrained=True).cuda()
mrgwd = MRGWD(latent_dim=512, output_size=(256, 256), use_vae=True, force_vae=True).cuda()

# Freeze backbone
for p in ulep.backbone.parameters():
    p.requires_grad = False

# Set up trainable parameters
trainable_params = []
if "encode_head" in TRAINABLE:
    for p in ulep.encode_head.parameters():
        p.requires_grad = True
    trainable_params.extend(list(ulep.encode_head.parameters()))
    print("✓ Training encode_head")

if "projector" in TRAINABLE:
    for p in mrgwd.latent_synth.projector.parameters():
        p.requires_grad = True
    trainable_params.extend(list(mrgwd.latent_synth.projector.parameters()))
    print("✓ Training projector")

# Freeze VAE and upsampler
for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

for p in mrgwd.upsample_net.parameters():
    p.requires_grad = False

print(f"Trainable params: {sum(p.numel() for p in trainable_params):,}")

optimizer = torch.optim.AdamW(trainable_params, lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.OneCycleLR(
    optimizer, max_lr=LR, epochs=NUM_EPOCHS, steps_per_epoch=100
)

# ============================================================
# LOAD UCF101 DATASET
# ============================================================
print(f"\n🔍 Loading from: {DATASET_PATH}")

video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Found {len(video_files)} videos")

# Extract frames from MORE videos
print("\n📂 Extracting frames...")
all_frames = []

NUM_VIDEOS = min(50, len(video_files))  # Use 50 videos for more diverse data
for i, video in enumerate(video_files[:NUM_VIDEOS]):
    if i % 10 == 0:
        print(f"  Processing video {i+1}/{NUM_VIDEOS}")
    frames = extract_frames(video, max_frames=8)
    all_frames.extend(frames)

print(f"Total frames: {len(all_frames)}")

if len(all_frames) == 0:
    print("ERROR: No frames extracted!")
    exit(1)

# ============================================================
# TRAINING LOOP
# ============================================================
print(f"\n🚀 Training {NUM_EPOCHS} epochs, batch_size={BATCH_SIZE}...")

ulep.train()
mrgwd.train()

criterion = nn.MSELoss()
scaler = torch.amp.GradScaler('cuda')

for epoch in range(NUM_EPOCHS):
    epoch_start = time.time()
    total_loss = 0
    num_batches = 0
    
    # Shuffle and create batches
    indices = torch.randperm(len(all_frames))
    
    for i in range(0, len(indices) - BATCH_SIZE + 1, BATCH_SIZE):
        batch_indices = indices[i:i + BATCH_SIZE]
        batch_frames = [all_frames[idx] for idx in batch_indices]
        
        # Preprocess batch
        x = torch.stack([preprocess_image(f) for f in batch_frames]).cuda()
        
        # Encode
        with torch.no_grad():
            features = ulep.backbone(x)
            target = torch.stack([
                torch.tensor(np.array(f.resize((256, 256))).astype(np.float32) / 127.5 - 1)
                for f in batch_frames
            ]).permute(0, 3, 1, 2).cuda()
        
        # Forward pass (with gradients)
        z = ulep.encode_head(features)
        z_norm = F.normalize(z, dim=-1)
        
        # Decode using direct_decode (gradient flow enabled)
        decoded = mrgwd.latent_synth(z_norm.float())
        
        # Resize if needed
        if decoded.shape != target.shape:
            decoded = F.interpolate(decoded, size=target.shape[-2:], mode='bilinear', align_corners=False)
        
        # Compute loss
        loss = criterion(decoded, target.float())
        
        # Backward pass
        optimizer.zero_grad()
        scaler.scale(loss).backward()
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(trainable_params, max_norm=GRAD_CLIP)
        scaler.step(optimizer)
        scaler.update()
        scheduler.step()
        
        total_loss += loss.item()
        num_batches += 1
    
    avg_loss = total_loss / max(num_batches, 1)
    elapsed = time.time() - epoch_start
    lr_now = optimizer.param_groups[0]['lr']
    
    print(f"Epoch {epoch+1:2d}/{NUM_EPOCHS} | Loss: {avg_loss:.4f} | LR: {lr_now:.6f} | Time: {elapsed:.1f}s")

print("\n✅ Training complete!")

# ============================================================
# EVALUATION
# ============================================================
print("\n🧪 Evaluating on held-out videos...")

ulep.eval()
mrgwd.eval()

gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
gtm_dec = GTMDecoder(qjl_proj_dim=64)

# Test on DIFFERENT videos (not used in training)
test_psnr = []
test_videos = video_files[NUM_VIDEOS:NUM_VIDEOS + 10]

for video in test_videos:
    frames = extract_frames(video, max_frames=5)
    for img in frames[:3]:
        x = preprocess_image(img).unsqueeze(0).cuda()
        
        with torch.no_grad():
            features = ulep.backbone(x)
            z = ulep.encode_head(features).squeeze(0)
            z_norm = F.normalize(z, dim=-1)
            
            # Encode/decode with GTM
            packets = gtm_enc.encode(z_norm.cpu())
            z_dec = gtm_dec.decode(packets, 512).cuda()
            
            # Decode
            dec = mrgwd.direct_decode(z_dec)
            
            target = torch.tensor(np.array(img.resize((256, 256))).astype(np.float32) / 127.5 - 1)
            target = target.permute(2, 0, 1)
            
            psnr = compute_psnr(target, dec.cpu())
            test_psnr.append(psnr)

print(f"\n📊 TEST PSNR: {np.mean(test_psnr):.2f} dB (std: {np.std(test_psnr):.2f})")

# ============================================================
# SAVE CHECKPOINT
# ============================================================
checkpoint_path = "/kaggle/working/omniquant_apex_weights.pt"
torch.save({
    "ulep_encode_head": ulep.encode_head.state_dict(),
    "mrgwd_projector": mrgwd.latent_synth.projector.state_dict(),
    "optimizer": optimizer.state_dict(),
}, checkpoint_path)
print(f"\n💾 Saved checkpoint to {checkpoint_path}")
