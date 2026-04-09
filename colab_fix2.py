# ============================================================
# FIXED - Proper path handling
# ============================================================

import shutil
import os
import sys

# First, let's find where we are and where the files are
print("Current directory:", os.getcwd())

# Find the project folder
possible_paths = [
    '/content/omniaquant',
    '/content/omniaquant/New Folder', 
    '/content/omniaquant/New Folder-main',
]

project_path = None
for p in possible_paths:
    if os.path.exists(p):
        # Check if ulep folder exists
        if os.path.exists(os.path.join(p, 'ulep')):
            project_path = p
            break

if project_path is None:
    # Search more broadly
    for root, dirs, files in os.walk('/content/omniaquant'):
        if 'ulep' in dirs:
            project_path = root
            break

print(f"Project path: {project_path}")

# Go to project directory
if project_path:
    os.chdir(project_path)
    print(f"Changed to: {os.getcwd()}")
    sys.path.insert(0, project_path)
else:
    print("❌ Could not find project folder!")

# ============================================================
# CHECK FILES EXIST
# ============================================================
print("\n📁 Checking key files:")
key_files = ['ulep/model.py', 'mrgwd/model.py', 'demo/run_gpu.py', 'gtm/codec.py']
for f in key_files:
    full_path = os.path.join(project_path or '.', f)
    if os.path.exists(full_path):
        print(f"  ✅ {f}")
    else:
        print(f"  ❌ {f} NOT FOUND")

# ============================================================
# INSTALL DEPENDENCIES
# ============================================================
!pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121 -q
!pip install transformers diffusers pillow numpy scipy timm -q
print("\n✅ Dependencies installed!")

# ============================================================
# TEST IMPORTS
# ============================================================
print("\n🔬 Testing imports...")
try:
    from ulep.model import ULEP
    print("  ✅ ulep.model imported")
except Exception as e:
    print(f"  ❌ ulep error: {e}")

try:
    from mrgwd.model import MRGWD
    print("  ✅ mrgwd.model imported")
except Exception as e:
    print(f"  ❌ mrgwd error: {e}")

# ============================================================
# RUN DEMO
# ============================================================
print("\n🚀 Running demo...")
!python demo/run_gpu.py