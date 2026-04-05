"""
OmniQuant-Apex Web Streaming Demo Server

FastAPI server with WebSocket endpoints for live encoder/decoder streaming.
Serves the frontend UI and provides REST endpoints for real-time config & metrics.

WebSocket protocol:
  - Client sends: JSON control messages or binary frame data
  - Server sends: binary decoded frame JPEG + JSON metadata frame
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import time
import io
import json
import threading
import base64
from typing import Optional, List, Dict, Any
from pathlib import Path
from collections import deque

import torch
import numpy as np
from PIL import Image

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
except ImportError:
    raise ImportError("Install FastAPI: pip install fastapi uvicorn[standard]")

from ulep.model import ULEP
from mrgwd.model import MRGWD
from codec.encoder import OmniQuantEncoder
from codec.decoder import OmniQuantDecoder
from codec.packets import serialize_packet, deserialize_packet
from demo.metrics import MetricsAccumulator, compute_psnr, compute_ssim


# =========================================================================
# Global Codec State
# =========================================================================

class CodecState:
    """Shared codec state with thread-safe access."""

    def __init__(self, latent_dim: int = 512, output_size=(512, 512)):
        self.latent_dim = latent_dim
        self.output_size = output_size
        self.lock = threading.Lock()

        # Models
        self.ulep_enc: Optional[ULEP] = None
        self.ulep_dec: Optional[ULEP] = None
        self.mrgwd: Optional[MRGWD] = None

        # Codec
        self.encoder: Optional[OmniQuantEncoder] = None
        self.decoder: Optional[OmniQuantDecoder] = None
        self.metrics = MetricsAccumulator(fps=30.0)

        # Config (adjustable at runtime)
        self.config = {
            "keyframe_interval": 30,
            "lcc_threshold": 0.15,
            "sparse_fraction": 0.25,
            "gtm_bits_kf": 6,
            "gtm_bits_pf": 3,
            "packet_loss_rate": 0.0,
            "use_pretrained": False,
        }

        # Stats buffer for streaming to frontend
        self.recent_stats: deque = deque(maxlen=300)
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        print("[Server] Initializing models...")
        use_pretrained = self.config["use_pretrained"]
        self.ulep_enc = ULEP(latent_dim=self.latent_dim, use_pretrained=use_pretrained)
        self.ulep_dec = ULEP(latent_dim=self.latent_dim, use_pretrained=use_pretrained)
        self.mrgwd = MRGWD(latent_dim=self.latent_dim,
                           output_size=self.output_size, use_vae=False)
        self.ulep_enc.eval()
        self.ulep_dec.eval()
        self.mrgwd.eval()
        self._rebuild_codec()
        self._initialized = True
        print("[Server] Models ready.")

    def _rebuild_codec(self):
        cfg = self.config
        self.encoder = OmniQuantEncoder(
            ulep=self.ulep_enc,
            latent_dim=self.latent_dim,
            keyframe_interval=cfg["keyframe_interval"],
            lcc_threshold=cfg["lcc_threshold"],
            sparse_fraction=cfg["sparse_fraction"],
            gtm_bits_keyframe=cfg["gtm_bits_kf"],
            gtm_bits_predictive=cfg["gtm_bits_pf"],
        )
        self.decoder = OmniQuantDecoder(
            ulep=self.ulep_dec,
            mrgwd=self.mrgwd,
            latent_dim=self.latent_dim,
            sparse_fraction=cfg["sparse_fraction"],
        )
        self.metrics = MetricsAccumulator(fps=30.0)

    def update_config(self, updates: dict):
        with self.lock:
            self.config.update(updates)
            if self._initialized:
                # Apply hot-reloadable settings
                if "lcc_threshold" in updates and self.encoder:
                    self.encoder.set_lcc_threshold(updates["lcc_threshold"])
                if "sparse_fraction" in updates and self.encoder:
                    self.encoder.set_sparse_fraction(updates["sparse_fraction"])


CODEC = CodecState(latent_dim=512, output_size=(512, 512))

# =========================================================================
# FastAPI App
# =========================================================================

app = FastAPI(title="OmniQuant-Apex Streaming Demo", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, CODEC.initialize)


@app.get("/", response_class=HTMLResponse)
async def root():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/api/status")
async def get_status():
    s = CODEC.metrics.summary()
    s["initialized"] = CODEC._initialized
    s["config"] = CODEC.config
    s["lcc_trigger_rate"] = (
        len(CODEC.metrics.lcc_trigger_indices) / max(1, len(CODEC.metrics.packet_bytes))
    )
    s["recent_bitrate_mbps"] = round(CODEC.metrics.instantaneous_bitrate_mbps, 4)
    return JSONResponse(s)


@app.post("/api/config")
async def update_config(body: dict):
    CODEC.update_config(body)
    return JSONResponse({"ok": True, "config": CODEC.config})


@app.post("/api/reset")
async def reset_codec():
    if CODEC._initialized:
        CODEC._rebuild_codec()
    return JSONResponse({"ok": True})


# =========================================================================
# WebSocket Endpoint — Streaming Encode/Decode
# =========================================================================

def tensor_to_jpeg_b64(t: torch.Tensor, quality: int = 85) -> str:
    """Convert (3,H,W) tensor in [-1,1] to base64 JPEG string."""
    arr = ((t.clamp(-1, 1) + 1) / 2 * 255).byte().permute(1, 2, 0).cpu().numpy()
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def pil_to_tensor(img: Image.Image) -> torch.Tensor:
    arr = np.array(img.resize((512, 512), Image.LANCZOS)).astype(np.float32) / 127.5 - 1.0
    return torch.tensor(arr).permute(2, 0, 1)


def generate_demo_frame(frame_idx: int) -> Image.Image:
    """Generate an animated synthetic frame for browser demo."""
    W, H = 512, 512
    t = (frame_idx % 300) / 300.0
    arr = np.zeros((H, W, 3), dtype=np.uint8)
    # Smooth animated gradient
    xs = np.linspace(0, 1, W)
    ys = np.linspace(0, 1, H)
    X, Y = np.meshgrid(xs, ys)
    r_ch = np.sin(2 * np.pi * (X + t)) * 0.5 + 0.5
    g_ch = np.cos(2 * np.pi * (Y + t * 1.3)) * 0.5 + 0.5
    b_ch = np.sin(2 * np.pi * (X * 0.7 + Y * 0.3 + t * 0.7)) * 0.5 + 0.5
    # Bouncing circle
    cx = int(W * (0.5 + 0.35 * np.cos(2 * np.pi * t)))
    cy = int(H * (0.5 + 0.35 * np.sin(2 * np.pi * t * 1.1)))
    arr[:, :, 0] = (r_ch * 220).astype(np.uint8)
    arr[:, :, 1] = (g_ch * 180).astype(np.uint8)
    arr[:, :, 2] = (b_ch * 200).astype(np.uint8)
    # Draw circle
    YY, XX = np.ogrid[:H, :W]
    mask = (XX - cx) ** 2 + (YY - cy) ** 2 < 3600
    arr[mask, 0] = 255
    arr[mask, 1] = int(200 * (1 - t))
    arr[mask, 2] = int(100 * t)
    return Image.fromarray(arr)


@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    """
    Main streaming WebSocket endpoint.
    Sends JSON messages with base64-encoded original + decoded frames.
    Client can send config update messages.
    """
    await websocket.accept()
    print("[WS] Client connected")

    if not CODEC._initialized:
        await websocket.send_json({"type": "error", "msg": "Models not initialized yet"})
        await websocket.close()
        return

    frame_idx = 0
    import asyncio

    try:
        while True:
            # Check for incoming client messages (non-blocking)
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=0.01)
                if msg.get("type") == "config":
                    CODEC.update_config(msg.get("data", {}))
                elif msg.get("type") == "reset":
                    CODEC._rebuild_codec()
                    frame_idx = 0
            except asyncio.TimeoutError:
                pass

            # Generate/load frame
            pil_frame = generate_demo_frame(frame_idx)

            # Simulate packet loss
            import random
            loss = random.random() < CODEC.config.get("packet_loss_rate", 0.0)

            t0 = time.perf_counter()
            if not loss:
                packet_bytes, enc_stats = CODEC.encoder.encode_frame(pil_frame)
                frame_tensor, dec_stats = CODEC.decoder.decode_packet(packet_bytes)
            else:
                # Simulate lost packet: use error concealment
                enc_stats = None
                frame_tensor, dec_stats = CODEC.decoder.conceal_one_frame()
                packet_bytes = b""

            latency_ms = (time.perf_counter() - t0) * 1000

            # Update metrics
            orig_t = pil_to_tensor(pil_frame)
            pb = len(packet_bytes) if packet_bytes else 0

            if enc_stats:
                CODEC.metrics.update(orig_t, frame_tensor.cpu(), pb,
                                     is_keyframe=enc_stats.is_keyframe,
                                     lcc_triggered=enc_stats.lcc_triggered)

            # Build response
            orig_b64 = tensor_to_jpeg_b64(orig_t)
            dec_b64 = tensor_to_jpeg_b64(frame_tensor.cpu())

            stats = CODEC.metrics.summary()
            msg = {
                "type": "frame",
                "frame_idx": frame_idx,
                "original": orig_b64,
                "decoded": dec_b64,
                "is_keyframe": enc_stats.is_keyframe if enc_stats else False,
                "is_concealed": loss,
                "lcc_triggered": enc_stats.lcc_triggered if enc_stats else False,
                "packet_bytes": pb,
                "latency_ms": round(latency_ms, 1),
                "bitrate_mbps": round(CODEC.metrics.instantaneous_bitrate_mbps, 4),
                "psnr": round(CODEC.metrics.psnr_values[-1] if CODEC.metrics.psnr_values else 0, 2),
                "ssim": round(CODEC.metrics.ssim_values[-1] if CODEC.metrics.ssim_values else 0, 4),
                "total_frames": stats["frames"],
                "keyframes": stats["keyframes"],
                "lcc_triggers": stats.get("lcc_triggers", 0),
                "delta_z_norm": enc_stats.delta_z_norm if enc_stats else 0,
                "energy_retention": enc_stats.energy_retention if enc_stats else 1.0,
                "sparse_k": enc_stats.sparse_k if enc_stats else 0,
            }
            await websocket.send_json(msg)

            frame_idx += 1
            await asyncio.sleep(1 / 30.0)   # 30 fps target

    except WebSocketDisconnect:
        print("[WS] Client disconnected")
    except Exception as e:
        print(f"[WS] Error: {e}")
        import traceback; traceback.print_exc()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
