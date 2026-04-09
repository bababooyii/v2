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
IMAGE_SIZE = 224
OUTPUT_SIZE = 256
BATCH_SIZE = 2
NUM_VIDEOS = 100
FRAMES_PER_VIDEO = 6
NUM_EPOCHS = 25
LR = 3e-5
GRAD_CLIP = 0.3
CHECKPOINT_PATH = "/kaggle/working/omniquant_apex_v4.pt"

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

ulep = ULEP(latent_dim=512, use_pretrained=True).cuda()
mrgwd = MRGWD(latent_dim=512, output_size=(OUTPUT_SIZE, OUTPUT_SIZE), use_vae=True, force_vae=True).cuda()

# GTM for quantization
gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
gtm_dec = GTMDecoder(qjl_proj_dim=64)

# Freeze
for p in ulep.backbone.parameters():
    p.requires_grad = False
for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

# Trainable: encode_head + projector
trainable = []
for p in ulep.encode_head.parameters():
    p.requires_grad = True
trainable += list(ulep.encode_head.parameters())

for p in mrgwd.latent_synth.projector.parameters():
    p.requires_grad = True
trainable += list(mrgwd.latent_synth.projector.parameters())

print(f"Trainable params: {sum(p.numel() for p in trainable):,}")

optimizer = torch.optim.AdamW(trainable, lr=LR, weight_decay=1e-4)
scheduler = torch.optim.lr_scheduler.OneCycleLR(
    optimizer, max_lr=LR, epochs=NUM_EPOCHS, steps_per_epoch=200
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
def preprocess(img, size=IMAGE_SIZE):
    img = img.resize((size, size), Image.LANCZOS)
    arr = np.array(img).astype(np.float32) / 255.0
    arr = (arr - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    return torch.from_numpy(arr).permute(2, 0, 1).float()

def target_tensor(img, size=OUTPUT_SIZE):
    arr = np.array(img.resize((size, size))).astype(np.float32) / 127.5 - 1.0
    return torch.from_numpy(arr).permute(2, 0, 1).float()

# ============================================================
# TRAINING WITH GTM IN THE LOOP
# ============================================================
print(f"\n🚀 Training {NUM_EPOCHS} epochs with GTM quantization...")

ulep.train()
mrgwd.train()

criterion = nn.MSELoss()
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
                
                x = torch.stack([preprocess(f) for f in batch_frames]).cuda()
                targets = torch.stack([target_tensor(f) for f in batch_frames]).cuda()
                
                # Encode
                features = ulep.backbone(x)
                z = ulep.encode_head(features)
                z_norm = F.normalize(z, dim=-1)
                
                # Process each sample through GTM individually
                decoded_list = []
                for j in range(z_norm.shape[0]):
                    z_single = z_norm[j:j+1]  # (1, 512)
                    z_cpu = z_single.detach().cpu()
                    packets = gtm_enc.encode(z_cpu)
                    z_q = gtm_dec.decode(packets, 512).cuda()
                    dec = mrgwd.latent_synth(z_q.float())
                    decoded_list.append(dec)
                
                decoded = torch.stack(decoded_list)
                
                if decoded.shape != targets.shape:
                    decoded = F.interpolate(decoded, size=targets.shape[-2:], mode='bilinear', align_corners=False)
                
                loss = criterion(decoded, targets.float())
                
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
        
        if (vi + 1) % 25 == 0:
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

print("\n✅ Training complete!")

# ============================================================
# EVALUATION
# ============================================================
print("\n🧪 Evaluating...")

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
            x = preprocess(img).unsqueeze(0).cuda()
            
            with torch.no_grad():
                features = ulep.backbone(x)
                z = ulep.encode_head(features).squeeze(0)
                z_norm = F.normalize(z, dim=-1)
                
                packets = gtm_enc.encode(z_norm.cpu())
                z_dec = gtm_dec.decode(packets, 512).cuda()
                
                dec = mrgwd.direct_decode(z_dec)
                target = target_tensor(img)
                
                psnr = compute_psnr(target, dec.cpu())
                psnr_vals.append(psnr)
                
    except Exception as e:
        print(f"Eval error: {e}")
        continue

if psnr_vals:
    print(f"\n📊 TEST PSNR: {np.mean(psnr_vals):.2f} dB (std: {np.std(psnr_vals):.2f})")
else:
    print("No PSNR computed")

print(f"\n💾 Checkpoint: {CHECKPOINT_PATH}")
