import sys, os
sys.path.insert(0, os.getcwd())

import torch
import numpy as np
from PIL import Image
import torch.nn.functional as F
import glob
import cv2

print(f"GPU: {torch.cuda.is_available()}")

from ulep.model import ULEP
from mrgwd.model import MRGWD
from gtm.codec import GTMEncoder, GTMDecoder
from demo.metrics import compute_psnr

# ============================================================
# LOAD UCF101 DATASET
# ============================================================
DATASET_PATH = "/kaggle/input/datasets/pevogam/ucf101"
print(f"\n🔍 Loading from: {DATASET_PATH}")

# Find video files
video_files = glob.glob(f"{DATASET_PATH}/**/*.avi", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mp4", recursive=True) + \
              glob.glob(f"{DATASET_PATH}/**/*.mkv", recursive=True)

print(f"Found {len(video_files)} videos")

if len(video_files) == 0:
    # Try to find directory structure
    print("Contents:", os.listdir(DATASET_PATH)[:20])
else:
    print("Sample videos:", video_files[:5])

# ============================================================
# EXTRACT FRAMES FROM VIDEOS
# ============================================================
def extract_frames(video_path, max_frames=10):
    """Extract frames from video"""
    frames = []
    try:
        cap = cv2.VideoCapture(video_path)
        count = 0
        while count < max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            # Convert BGR to RGB
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(frame))
            count += 1
        cap.release()
    except Exception as e:
        print(f"Error reading {video_path}: {e}")
    return frames

# Load some training frames
print("\n📂 Extracting frames from videos...")
all_frames = []

# Take first 10 videos
for i, video in enumerate(video_files[:10]):
    print(f"Processing video {i+1}/10: {os.path.basename(video)}")
    frames = extract_frames(video, max_frames=10)
    all_frames.extend(frames)
    print(f"  Extracted {len(frames)} frames")

print(f"\nTotal frames: {len(all_frames)}")

if len(all_frames) == 0:
    print("No frames extracted! Trying a different approach...")
    # Maybe it's images not videos
    image_files = glob.glob(f"{DATASET_PATH}/**/*.jpg", recursive=True) + \
                   glob.glob(f"{DATASET_PATH}/**/*.png", recursive=True)
    print(f"Found {len(image_files)} images instead")
    
    # Just use first 50 images
    for img_path in image_files[:50]:
        try:
            all_frames.append(Image.open(img_path).convert('RGB').resize((256, 256)))
        except:
            pass
    
    print(f"Loaded {len(all_frames)} images")

# ============================================================
# LOAD MODELS
# ============================================================
print("\n🤖 Loading models...")

ulep = ULEP(latent_dim=512, use_pretrained=True).cuda()
mrgwd = MRGWD(latent_dim=512, output_size=(256,256), use_vae=True, force_vae=True).cuda()

# Freeze backbone
for p in ulep.backbone.parameters():
    p.requires_grad = False
for p in mrgwd.latent_synth.projector.parameters():
    p.requires_grad = False
for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

# Only train encode_head
for p in ulep.encode_head.parameters():
    p.requires_grad = True

optimizer = torch.optim.Adam(ulep.encode_head.parameters(), lr=1e-3)

print("Trainable params:", sum(p.numel() for p in ulep.encode_head.parameters() if p.requires_grad))

def preprocess_on_gpu(img):
    img = img.resize((224, 224), Image.LANCZOS)
    arr = np.array(img).astype(np.float32) / 255.0
    arr = (arr - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    return torch.from_numpy(arr).permute(2,0,1).unsqueeze(0).cuda()

# ============================================================
# TRAIN WITH REAL VIDEO FRAMES
# ============================================================
print("\n🚀 Training with REAL video frames...")

N_TRAIN = min(50, len(all_frames))
N_EPOCHS = 5

for epoch in range(N_EPOCHS):
    total_loss = 0
    ulep.encode_head.train()
    
    # Shuffle frames each epoch
    indices = np.random.permutation(len(all_frames))[:N_TRAIN]
    
    for idx in indices:
        img = all_frames[idx]
        
        x = preprocess_on_gpu(img)
        with torch.no_grad():
            features = ulep.backbone(x)
        
        z = ulep.encode_head(features).squeeze(0)
        z_norm = z / (z.norm(dim=-1, keepdim=True) + 1e-8)
        
        with torch.no_grad():
            dec = mrgwd.synthesize(z_norm)
        
        target = torch.tensor(np.array(img.resize((256,256))).astype(np.float32)/127.5-1).permute(2,0,1).cuda()
        
        if dec.shape != target.shape:
            dec = F.interpolate(dec.unsqueeze(0), size=target.shape[-2:], mode='bilinear').squeeze(0)
        
        loss = F.mse_loss(dec, target)
        
        if torch.isnan(loss):
            continue
        
        optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(ulep.encode_head.parameters(), max_norm=1.0)
        optimizer.step()
        
        total_loss += loss.item()
    
    print(f"Epoch {epoch+1}/{N_EPOCHS}: Loss = {total_loss/N_TRAIN:.4f}")

print("\n✅ Training complete!")

# ============================================================
# TEST WITH NEW FRAMES
# ============================================================
print("\n🧪 Testing with real video frames...")

# Get different videos for testing
test_frames = []
for video in video_files[10:15]:  # Different videos
    frames = extract_frames(video, max_frames=5)
    test_frames.extend(frames)

gtm_enc = GTMEncoder(n_bits=6, qjl_proj_dim=64)
gtm_dec = GTMDecoder(qjl_proj_dim=64)

ulep.eval()
mrgwd.eval()

psnr_vals = []

for i, img in enumerate(test_frames[:20]):
    x = preprocess_on_gpu(img)
    with torch.no_grad():
        features = ulep.backbone(x)
        z = ulep.encode_head(features).squeeze(0)
        z_norm = z / (z.norm(dim=-1, keepdim=True) + 1e-8)
    
    packets = gtm_enc.encode(z_norm.cpu())
    z_dec = gtm_dec.decode(packets, 512).cuda()
    
    with torch.no_grad():
        dec = mrgwd.synthesize(z_dec)
    
    target = torch.tensor(np.array(img.resize((256,256))).astype(np.float32)/127.5-1).permute(2,0,1)
    psnr = compute_psnr(target, dec.cpu())
    psnr_vals.append(psnr)
    
    print(f"Frame {i}: PSNR={psnr:.1f}dB")

print(f"\n📊 REAL DATA RESULT: {np.mean(psnr_vals):.1f} dB")
print("\n🎉 This is using REAL video frames from UCF101!")