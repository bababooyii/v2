# ============================================================
# FIX FOR FILES WITH SPACES IN NAME
# ============================================================

# Your file has a space in the name: "New Folder.zip"
# We need to handle that carefully

# Copy with proper quoting
import shutil

source = '/content/drive/MyDrive/New Folder.zip'  # <-- Your file
dest = '/content/omniaquant.zip'

shutil.copy(source, dest)
print(f"✅ Copied to: {dest}")

# Extract with proper quoting
extract_path = '/content/omniaquant'
!mkdir -p "{extract_path}"
!unzip -q "{dest}" -d "{extract_path}"

print(f"✅ Extracted to: {extract_path}")

# Find the folder - it might be "New Folder" or "New Folder-main" etc
import os

print("\n📁 Contents of extracted folder:")
for item in os.listdir(extract_path):
    print(f"  - {item}")
    
    # If it's a folder, go into it
    item_path = os.path.join(extract_path, item)
    if os.path.isdir(item_path):
        %cd {item_path}
        print(f"✅ Now in: {os.getcwd()}")

# ============================================================
# INSTALL DEPENDENCIES
# ============================================================
print("\n📦 Installing dependencies...")
!pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121 -q
!pip install transformers diffusers pillow numpy scipy timm -q
print("✅ Installed!")

# ============================================================
# RUN DEMO
# ============================================================
print("\n🚀 Running demo...")
import sys
sys.path.insert(0, os.getcwd())
!python demo/run_gpu.py