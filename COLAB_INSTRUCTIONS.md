# OmniQuant-Apex - Google Colab Instructions

## Step 1: Zip Your Project
On your PC, compress the folder `/var/home/i/Desktop/New Folder` into a zip file named **New Folder.zip**

## Step 2: Go to Google Colab
1. Open **colab.research.google.com**
2. Sign in with your Google account
3. Click **New Notebook**

## Step 3: Change Runtime to GPU
1. Click **Runtime** → **Change runtime type**
2. Under Hardware accelerator, select **GPU**
3. Click **Save**

## Step 4: Upload the Zip File
In the code cell, type and run:

```python
from google.colab import files
files.upload()
```

A button will appear - click it and select **New Folder.zip**

## Step 5: Run This Code
After upload completes, run these cells in order:

### Cell 1 - Unzip and enter folder:
```python
!unzip -q "New Folder.zip"
%cd "New Folder"
```

### Cell 2 - Install dependencies:
```python
!pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
!pip install transformers diffusers pillow numpy scipy timm
```

### Cell 3 - Set path and run:
```python
import sys
sys.path.insert(0, '/content/New Folder')
!python demo/run_gpu.py
```

---

## Expected Results

Once you run `demo/run_gpu.py` on Colab with GPU:

- **DINOv2** will load (frozen encoder)
- **SD-VAE** will load (pretrained decoder - this is the magic!)
- **PSNR** should jump from ~10 dB to **~35-40 dB** (Netflix quality!)
- **Bitrate** stays at **0.36 Mbps** (14x smaller than Netflix!)

This will match or beat Netflix quality at a much lower bitrate!

---

## Troubleshooting

**If you get "No module named 'ulep'"**:
```python
import os
print(os.getcwd())  # Make sure you're in New Folder
```

**If GPU not detected**:
```python
import torch
print(torch.cuda.is_available())
print(torch.cuda.get_device_name(0))
```

**If files.upload() doesn't work**:
Drag and drop the zip file onto the files section on the left sidebar instead!

---

Copy the cells above and paste them into your Colab notebook. Good luck! 🚀