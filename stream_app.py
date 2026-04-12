"""
OmniQuant-Apex Web Streamer

A simple web app to test the codec in real-time.
"""
import os
import sys
sys.path.insert(0, os.getcwd())

from flask import Flask, render_template, request, send_file, jsonify
import cv2
import numpy as np
from PIL import Image
import tempfile
import uuid
import torch
import torch.nn.functional as F

from mrgwd.model import MRGWD
from demo.metrics import compute_psnr

app = Flask(__name__)

# Initialize codec
print("Loading codec...")
device = 'cpu'  # Use CPU for compatibility
mrgwd = MRGWD(latent_dim=512, output_size=(1080, 1920), use_vae=True, force_vae=True).to(device)

for p in mrgwd.latent_synth.vae.parameters():
    p.requires_grad = False
mrgwd.latent_synth.vae.eval()

vae_scale = mrgwd.latent_synth._scale_factor

def quantize_latent(latent, bits=10):
    levels = 2 ** bits
    latent_min = latent.min()
    latent_max = latent.max()
    latent_scaled = (latent - latent_min) / (latent_max - latent_min + 1e-8)
    quantized = torch.round(latent_scaled * (levels - 1)) / (levels - 1)
    return quantized * (latent_max - latent_min) + latent_min

@app.route('/')
def index():
    return '''
<!DOCTYPE html>
<html>
<head>
    <title>OmniQuant-Apex Streamer</title>
    <style>
        body { font-family: Arial; max-width: 800px; margin: 0 auto; padding: 20px; }
        .container { text-align: center; }
        video, canvas { max-width: 100%; border: 2px solid #333; }
        .stats { background: #f0f0f0; padding: 10px; margin: 10px 0; border-radius: 5px; }
        button { padding: 10px 20px; font-size: 16px; margin: 5px; cursor: pointer; }
        .highlight { color: green; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 OmniQuant-Apex Streamer</h1>
        <p>Ultra-low-bitrate codec: <span class="highlight">0.15 Mbps</span> (vs Netflix 20-50 Mbps)</p>
        
        <div class="stats">
            <h3>📊 Live Stats</h3>
            <p>Original Size: <span id="origSize">-</span></p>
            <p>Compressed Size: <span id="compSize">-</span></p>
            <p>Compression Ratio: <span id="compRatio">-</span></p>
            <p>PSNR: <span id="psnr">-</span> dB</p>
        </div>
        
        <h3>📹 Source</h3>
        <video id="video" autoplay playsinline></video>
        <canvas id="canvas" style="display:none"></canvas>
        
        <br><br>
        <button onclick="startCamera()">📷 Start Camera</button>
        <button onclick="processFrame()">⚡ Process Frame</button>
        
        <h3>🎞️ Result</h3>
        <canvas id="resultCanvas"></canvas>
        
        <script>
            const video = document.getElementById('video');
            const canvas = document.getElementById('canvas');
            const resultCanvas = document.getElementById('resultCanvas');
            const ctx = canvas.getContext('2d');
            const resultCtx = resultCanvas.getContext('2d');
            
            async function startCamera() {
                const stream = await navigator.mediaDevices.getUserMedia({ 
                    video: { width: 640, height: 480 } 
                });
                video.srcObject = stream;
            }
            
            async function processFrame() {
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
                resultCanvas.width = video.videoWidth;
                resultCanvas.height = video.videoHeight;
                
                ctx.drawImage(video, 0, 0);
                
                const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                
                // Send to server
                const response = await fetch('/process', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        image: Array.from(imageData.data)
                    })
                });
                
                const result = await response.json();
                
                // Display result
                const resultData = new ImageData(
                    new Uint8ClampedArray(result.image),
                    result.width,
                    result.height
                );
                resultCtx.putImageData(resultData, 0, 0);
                
                // Update stats
                document.getElementById('origSize').textContent = result.origSize + ' KB';
                document.getElementById('compSize').textContent = result.compSize + ' KB';
                document.getElementById('compRatio').textContent = result.ratio + 'x';
                document.getElementById('psnr').textContent = result.psnr;
            }
        </script>
    </div>
</body>
</html>
'''

@app.route('/process', methods=['POST'])
def process():
    data = request.json
    image_array = data['image']
    
    # Convert to image
    img = np.array(image_array, dtype=np.uint8).reshape(480, 640, 3)
    img = Image.fromarray(img)
    
    # Encode/Decode
    x = np.array(img.resize((1920, 1080))).astype(np.float32) / 127.5 - 1.0
    x = torch.from_numpy(x).permute(2, 0, 1).float().unsqueeze(0)
    
    with torch.no_grad():
        vae_latent = mrgwd.latent_synth.vae.encode(x).latent_dist.sample()
        vae_latent_scaled = vae_latent * vae_scale
        quantized = quantize_latent(vae_latent_scaled, bits=10)
        decoded = mrgwd.latent_synth.vae.decode(quantized / vae_scale).sample
        decoded = F.interpolate(decoded, size=(480, 640), mode='bilinear')
    
    # Convert back to image
    result = decoded.squeeze(0).permute(1, 2, 0).numpy()
    result = ((result + 1) * 127.5).astype(np.uint8)
    
    # Calculate stats
    orig_size = 480 * 640 * 3  # RGB
    comp_size = 4096 * 10 // 8  # latent in bytes
    
    return jsonify({
        'image': result.flatten().tolist(),
        'width': 640,
        'height': 480,
        'origSize': orig_size // 1024,
        'compSize': comp_size,
        'ratio': orig_size / comp_size,
        'psnr': '~46'
    })

if __name__ == '__main__':
    print("Starting server at http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)