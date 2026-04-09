# Run this in Google Colab

# ============================================================
# STEP 1: Mount Google Drive
# ============================================================
from google.colab import drive
drive.mount('/content/drive')

# ============================================================
# STEP 2: Set your zip file path
# ============================================================
# Replace this with YOUR actual zip file path in Google Drive
# Look at your Drive folder to find the exact path

# Example paths - choose the one that matches YOUR file:
# Option A: If you uploaded directly to Drive root:
zip_file_path = '/content/drive/MyDrive/New Folder.zip'

# Option B: If it's in a subfolder:
# zip_file_path = '/content/drive/MyDrive/folder_name/New Folder.zip'

# Option C: If the file has a different name, change it here:
# zip_file_path = '/content/drive/MyDrive/your_actual_filename.zip'

# ============================================================
# STEP 3: Extract the zip file
# ============================================================
extract_path = '/content/omniaquant'

# Create the extraction directory
!mkdir -p {extract_path}

# Unzip the file
!unzip -q "{zip_file_path}" -d {extract_path}

print(f"✅ Extracted to: {extract_path}")

# Show what was extracted
print("\n📁 Contents:")
!ls -la {extract_path}

# ============================================================
# STEP 4: Go to the extracted folder
# ============================================================
%cd {extract_path}

# Find the actual folder name (might be "New Folder" or similar)
import os
for item in os.listdir(extract_path):
    print(f"  Found: {item}")
    if os.path.isdir(os.path.join(extract_path, item)):
        %cd {os.path.join(extract_path, item)}
        print(f"  ✅ Now in: {os.getcwd()}")
        break

# ============================================================
# STEP 5: Install dependencies
# ============================================================
!pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
!pip install transformers diffusers pillow numpy scipy timm

print("\n✅ Dependencies installed!")

# ============================================================
# STEP 6: Run the demo
# ============================================================
import sys
sys.path.insert(0, os.getcwd())

!python demo/run_gpu.py