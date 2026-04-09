# ============================================================
# MOUNT DRIVE (you already did this)
# ============================================================
from google.colab import drive
drive.mount('/content/drive')

# ============================================================
# FIND YOUR ZIP FILE PATH
# ============================================================
# Run this to see your Drive files:
import os

print("📂 Your Google Drive contents:")
drive_path = '/content/drive/MyDrive'

# List files
for root, dirs, files in os.walk(drive_path):
    for f in files:
        if f.endswith('.zip'):
            full_path = os.path.join(root, f)
            print(f"  Found zip: {full_path}")

# ============================================================
# COPY YOUR ZIP FILE TO LOCAL STORAGE (faster)
# ============================================================
# Based on what you find above, set this variable:

# EXAMPLE - change 'YOUR_FILENAME.zip' to your actual file:
zip_file_path = '/content/drive/MyDrive/YOUR_FILENAME.zip'

# Copy to local (faster than extracting from Drive)
import shutil
local_zip = '/content/New Folder.zip'
shutil.copy(zip_file_path, local_zip)
print(f"✅ Copied to: {local_zip}")

# ============================================================
# EXTRACT
# ============================================================
extract_path = '/content/omniaquant'
!mkdir -p {extract_path}
!unzip -q {local_zip} -d {extract_path}

# Find the actual folder
for item in os.listdir(extract_path):
    print(f"📁 Extracted: {item}")
    project_folder = os.path.join(extract_path, item)
    if os.path.isdir(project_folder):
        %cd {project_folder}
        print(f"✅ Working in: {os.getcwd()}")
        break

# ============================================================
# INSTALL DEPENDENCIES
# ============================================================
!pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
!pip install transformers diffusers pillow numpy scipy timm

# ============================================================
# RUN DEMO
# ============================================================
import sys
sys.path.insert(0, os.getcwd())
!python demo/run_gpu.py