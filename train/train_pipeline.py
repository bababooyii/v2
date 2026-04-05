"""
OmniQuant-Apex Training Pipeline

End-to-end training for ULEP + MR-GWD with:
  - Joint reconstruction + prediction + perceptual + temporal losses
  - Multi-GPU DDP support
  - Mixed precision (AMP)
  - Gradient accumulation
  - Learning rate warmup + cosine decay
  - Checkpointing + evaluation
  - ONNX export for Rust inference

Usage:
  python train/train_pipeline.py --data /path/to/videos --epochs 100
"""
import os
import sys
import time
import math
import json
import argparse
from pathlib import Path
from typing import Optional, Dict, Any, List

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, Dataset
from torch.cuda.amp import autocast, GradScaler
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR, LinearLR, SequentialLR

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ulep.model import ULEP
from mrgwd.model import MRGWD
from mrgwd.latent_diffusion import LatentDiffusionSynth
from mrgwd.upsampling import TemporalUpsampleNet


# ================================================================
# Dataset
# ================================================================

class VideoFrameDataset(Dataset):
    """
    Loads video files and returns consecutive frame triplets (F_{t-2}, F_{t-1}, F_t).
    Supports:
      - Directory of video files (mp4, avi, mkv)
      - Image sequences (frames in folders)
      - Synthetic data for testing
    """

    def __init__(
        self,
        data_path: str,
        seq_len: int = 4,
        frame_size: tuple = (224, 224),
        max_videos: int = -1,
        max_frames_per_video: int = 300,
        transform=None,
    ):
        self.seq_len = seq_len
        self.frame_size = frame_size
        self.transform = transform
        self.samples = []

        video_files = self._find_videos(data_path, max_videos)

        for vf in video_files:
            frames = self._load_video(vf, max_frames_per_video)
            # Create overlapping sequences
            for i in range(len(frames) - seq_len + 1):
                self.samples.append(frames[i : i + seq_len])

        if not self.samples:
            # Fallback: generate synthetic video data
            print("[Dataset] No videos found, generating synthetic data...")
            self._generate_synthetic(200, seq_len)

    def _find_videos(self, path: str, max_videos: int) -> List[Path]:
        p = Path(path)
        if p.is_file():
            return [p]
        if p.is_dir():
            exts = {".mp4", ".avi", ".mkv", ".webm", ".mov"}
            files = sorted(
                [f for f in p.rglob("*") if f.suffix.lower() in exts]
            )
            if max_videos > 0:
                files = files[:max_videos]
            return files
        return []

    def _load_video(self, path: Path, max_frames: int):
        """Load frames from video using OpenCV."""
        try:
            import cv2
            cap = cv2.VideoCapture(str(path))
            frames = []
            while len(frames) < max_frames:
                ret, frame = cap.read()
                if not ret:
                    break
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil = self._resize_frame(frame)
                frames.append(pil)
            cap.release()
            return frames
        except Exception as e:
            print(f"[!] Failed to load {path}: {e}")
            return []

    def _resize_frame(self, frame):
        from PIL import Image
        return Image.fromarray(frame).resize(self.frame_size, Image.LANCZOS)

    def _generate_synthetic(self, n_videos: int, seq_len: int):
        """Generate synthetic video data for testing the pipeline."""
        import numpy as np
        for v in range(n_videos):
            frames = []
            for i in range(30):
                arr = np.zeros((*self.frame_size, 3), dtype=np.uint8)
                t = (v * 30 + i) / (n_videos * 30)
                xs = np.linspace(0, 1, self.frame_size[1])
                ys = np.linspace(0, 1, self.frame_size[0])
                X, Y = np.meshgrid(xs, ys)
                arr[:, :, 0] = (np.sin(2 * np.pi * (X + t)) * 0.5 + 0.5) * 255
                arr[:, :, 1] = (np.cos(2 * np.pi * (Y + t * 1.3)) * 0.5 + 0.5) * 255
                arr[:, :, 2] = (np.sin(2 * np.pi * (X * 0.7 + Y * 0.3 + t * 0.7)) * 0.5 + 0.5) * 255
                frames.append(Image.fromarray(arr.astype(np.uint8)))
            for i in range(len(frames) - seq_len + 1):
                self.samples.append(frames[i : i + seq_len])

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        frames = self.samples[idx]
        # Convert to tensors: (seq_len, 3, H, W) in [0, 1]
        import torchvision.transforms as T
        to_tensor = T.ToTensor()
        tensors = [to_tensor(f) for f in frames]
        return torch.stack(tensors)  # (seq_len, 3, H, W)


# ================================================================
# Loss Functions
# ================================================================

class OmniQuantLoss(nn.Module):
    """
    Joint loss for ULEP + MR-GWD training.

    Components:
      L_recon     = ||F̂_t - F_t||_1                          (reconstruction)
      L_predict   = ||ẑ_t - z_t||_2                          (prediction)
      L_percept   = LPIPS(F̂_t, F_t)                          (perceptual)
      L_temporal  = ||F̂_t - warp(F̂_{t-1}, flow)||_1          (temporal consistency)
      L_latent    = ||z_t||_2 regularization                  (latent compactness)
      L_codebook  = commitment loss for quantization          (if applicable)
    """

    def __init__(
        self,
        lambda_recon: float = 1.0,
        lambda_predict: float = 0.5,
        lambda_percept: float = 0.1,
        lambda_temporal: float = 0.3,
        lambda_latent: float = 0.01,
        use_lpips: bool = True,
    ):
        super().__init__()
        self.lambda_recon = lambda_recon
        self.lambda_predict = lambda_predict
        self.lambda_percept = lambda_percept
        self.lambda_temporal = lambda_temporal
        self.lambda_latent = lambda_latent

        # LPIPS perceptual loss (optional)
        self.use_lpips = use_lpips and self._lpips_available()
        if self.use_lpips:
            import lpips
            self.lpips_model = lpips.LPIPS(net="vgg").eval()
            for p in self.lpips_model.parameters():
                p.requires_grad_(False)

    def _lpips_available(self):
        try:
            import lpips
            return True
        except ImportError:
            return False

    def forward(
        self,
        z_t: torch.Tensor,
        z_hat_t: Optional[torch.Tensor],
        F_hat_t: torch.Tensor,
        F_t: torch.Tensor,
        F_hat_prev: Optional[torch.Tensor] = None,
        flow: Optional[torch.Tensor] = None,
    ) -> Dict[str, torch.Tensor]:
        losses = {}

        # 1. Reconstruction loss (L1)
        losses["recon"] = F.l1_loss(F_hat_t, F_t)

        # 2. Prediction loss (if predictor active)
        if z_hat_t is not None:
            losses["predict"] = F.mse_loss(z_hat_t, z_t)
        else:
            losses["predict"] = torch.tensor(0.0, device=z_t.device)

        # 3. Perceptual loss (LPIPS)
        if self.use_lpips and self.lambda_percept > 0:
            losses["percept"] = self.lpips_model(F_hat_t, F_t).mean()
        else:
            # Fallback: L2 in pixel space as perceptual proxy
            losses["percept"] = F.mse_loss(F_hat_t, F_t)

        # 4. Temporal consistency loss
        if F_hat_prev is not None and flow is not None:
            from mrgwd.upsampling import warp_frame
            warped = warp_frame(F_hat_prev, flow)
            losses["temporal"] = F.l1_loss(F_hat_t, warped)
        else:
            losses["temporal"] = torch.tensor(0.0, device=z_t.device)

        # 5. Latent compactness (encourage unit-norm latents)
        losses["latent"] = (z_t.norm(dim=-1) - 1.0).pow(2).mean()

        # Total weighted loss
        total = (
            self.lambda_recon * losses["recon"]
            + self.lambda_predict * losses["predict"]
            + self.lambda_percept * losses["percept"]
            + self.lambda_temporal * losses["temporal"]
            + self.lambda_latent * losses["latent"]
        )
        losses["total"] = total

        return losses


# ================================================================
# Training Engine
# ================================================================

class TrainingEngine:
    """
    Manages the full training loop with:
      - DDP for multi-GPU
      - AMP for mixed precision
      - Gradient accumulation
      - LR warmup + cosine decay
      - Checkpointing
      - Evaluation
      - ONNX export
    """

    def __init__(
        self,
        ulep: ULEP,
        mrgwd: MRGWD,
        optimizer: torch.optim.Optimizer,
        scheduler: torch.optim.lr_scheduler._LRScheduler,
        loss_fn: OmniQuantLoss,
        device: torch.device,
        gradient_accumulation_steps: int = 1,
        use_amp: bool = True,
        is_distributed: bool = False,
        rank: int = 0,
        output_dir: str = "checkpoints",
    ):
        self.ulep = ulep
        self.mrgwd = mrgwd
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.loss_fn = loss_fn
        self.device = device
        self.gradient_accumulation_steps = gradient_accumulation_steps
        self.use_amp = use_amp
        self.is_distributed = is_distributed
        self.rank = rank
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.scaler = GradScaler() if use_amp else None
        self.global_step = 0
        self.epoch = 0
        self.best_loss = float("inf")

    def train_step(self, batch: torch.Tensor) -> Dict[str, float]:
        """
        Single training step.

        batch: (batch_size, seq_len, 3, H, W)
        """
        batch = batch.to(self.device)
        batch_size, seq_len = batch.shape[:2]

        total_losses = {}

        for step in range(self.gradient_accumulation_steps):
            # Get sub-batch
            sub_size = batch_size // self.gradient_accumulation_steps
            start = step * sub_size
            end = start + sub_size
            sub_batch = batch[start:end]

            with autocast(enabled=self.use_amp):
                step_losses = self._forward_pass(sub_batch)

                loss = step_losses["total"] / self.gradient_accumulation_steps

            # Backward
            if self.scaler:
                self.scaler.scale(loss).backward()
            else:
                loss.backward()

            # Accumulate losses for logging
            for k, v in step_losses.items():
                total_losses[k] = total_losses.get(k, 0.0) + v.item()

        # Optimizer step
        if self.scaler:
            self.scaler.unscale_(self.optimizer)
            torch.nn.utils.clip_grad_norm_(
                list(self.ulep.parameters()) + list(self.mrgwd.parameters()),
                max_norm=1.0,
            )
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            torch.nn.utils.clip_grad_norm_(
                list(self.ulep.parameters()) + list(self.mrgwd.parameters()),
                max_norm=1.0,
            )
            self.optimizer.step()
        self.optimizer.zero_grad()
        self.scheduler.step()

        # Average losses
        for k in total_losses:
            total_losses[k] /= self.gradient_accumulation_steps

        self.global_step += 1
        return total_losses

    def _forward_pass(self, batch: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Forward pass for a batch of frame sequences.

        For each timestep t in the sequence:
          1. Encode F_t → z_t
          2. Predict ẑ_t from z_{t-1}, z_{t-2}
          3. Decode z_t → F̂_t
          4. Compute losses
        """
        batch_size, seq_len = batch.shape[:2]
        device = batch.device

        # Flatten for ULEP encoding: (batch * seq, 3, H, W)
        flat_frames = batch.view(-1, *batch.shape[2:])

        # Encode all frames
        z_all = self.ulep.encode_trainable(flat_frames)  # (batch*seq, D)
        z_all = z_all.view(batch_size, seq_len, -1)  # (batch, seq, D)

        # Decode all frames
        z_flat = z_all.view(-1, z_all.shape[-1])
        F_hat_all = self.mrgwd.synthesize_batch(z_flat)  # (batch*seq, 3, H, W)
        F_hat_all = F_hat_all.view(batch_size, seq_len, *batch.shape[2:])

        # Compute losses for each timestep (skip first 2 for prediction)
        total_loss = torch.tensor(0.0, device=device)
        loss_dict = {
            "total": torch.tensor(0.0, device=device),
            "recon": torch.tensor(0.0, device=device),
            "predict": torch.tensor(0.0, device=device),
            "percept": torch.tensor(0.0, device=device),
            "temporal": torch.tensor(0.0, device=device),
            "latent": torch.tensor(0.0, device=device),
        }
        count = 0

        for t in range(2, seq_len):
            z_t = z_all[:, t]  # (B, D)
            F_t = batch[:, t]  # (B, 3, H, W)
            F_hat_t = F_hat_all[:, t]

            # Prediction
            z_hat_t = self.ulep.predictor_head(z_all[:, t - 1], z_all[:, t - 2])

            # Temporal
            F_hat_prev = F_hat_all[:, t - 1] if t > 2 else None

            losses = self.loss_fn(
                z_t=z_t,
                z_hat_t=z_hat_t,
                F_hat_t=F_hat_t,
                F_t=F_t,
                F_hat_prev=F_hat_prev,
                flow=None,  # Flow estimation is expensive; skip during training
            )

            for k in loss_dict:
                loss_dict[k] = loss_dict[k] + losses[k]
            count += 1

        # Average over timesteps
        for k in loss_dict:
            loss_dict[k] /= max(count, 1)

        return loss_dict

    def train_epoch(self, dataloader: DataLoader) -> Dict[str, float]:
        """Train for one epoch."""
        self.ulep.train()
        self.mrgwd.train()

        epoch_losses = {}
        n_batches = 0
        t0 = time.time()

        for i, batch in enumerate(dataloader):
            losses = self.train_step(batch)

            for k, v in losses.items():
                epoch_losses[k] = epoch_losses.get(k, 0.0) + v
            n_batches += 1

            if i % 10 == 0 and self.rank == 0:
                lr = self.scheduler.get_last_lr()[0]
                elapsed = time.time() - t0
                print(
                    f"  Step {self.global_step:6d} | "
                    f"Loss: {losses['total']:.4f} | "
                    f"Recon: {losses['recon']:.4f} | "
                    f"Predict: {losses['predict']:.4f} | "
                    f"LR: {lr:.6f} | "
                    f"{elapsed:.1f}s"
                )

        # Average
        for k in epoch_losses:
            epoch_losses[k] /= max(n_batches, 1)

        return epoch_losses

    @torch.no_grad()
    def evaluate(self, dataloader: DataLoader) -> Dict[str, float]:
        """Evaluate on validation set."""
        self.ulep.eval()
        self.mrgwd.eval()

        eval_losses = {}
        n_batches = 0

        for batch in dataloader:
            batch = batch.to(self.device)
            losses = self._forward_pass(batch)

            for k, v in losses.items():
                eval_losses[k] = eval_losses.get(k, 0.0) + v.item()
            n_batches += 1

        for k in eval_losses:
            eval_losses[k] /= max(n_batches, 1)

        return eval_losses

    def save_checkpoint(self, name: str, metrics: Dict[str, float]):
        """Save model checkpoint."""
        if self.is_distributed and self.rank != 0:
            return

        ckpt = {
            "global_step": self.global_step,
            "epoch": self.epoch,
            "metrics": metrics,
            "ulep_encode_head": self.ulep.encode_head.state_dict(),
            "ulep_predictor_head": self.ulep.predictor_head.state_dict(),
            "mrgwd_latent_synth": self.mrgwd.latent_synth.projector.state_dict(),
            "mrgwd_upsample": self.mrgwd.upsample_net.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "scheduler": self.scheduler.state_dict(),
        }

        path = self.output_dir / f"{name}.pt"
        torch.save(ckpt, path)
        print(f"  Saved checkpoint: {path}")

        # Also save as "latest"
        torch.save(ckpt, self.output_dir / "latest.pt")

    def export_onnx(self, latent_dim: int = 512, output_dir: str = "onnx_models"):
        """Export models to ONNX for Rust inference."""
        if self.is_distributed and self.rank != 0:
            return

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        device = self.device

        self.ulep.eval()
        self.mrgwd.eval()

        # Export ULEP encode
        dummy_frame = torch.randn(1, 3, 224, 224, device=device)
        encode_path = out / "ulep_encode.onnx"

        torch.onnx.export(
            self.ulep.encode_head,
            (torch.randn(1, 196, 384, device=device),),
            str(encode_path),
            input_names=["features"],
            output_names=["z_t"],
            dynamic_axes={"features": {1: "n_patches"}, "z_t": {0: "batch"}},
            opset_version=17,
        )
        print(f"  Exported ULEP encode: {encode_path}")

        # Export ULEP predictor
        pred_path = out / "ulep_predictor.onnx"
        dummy_z1 = torch.randn(1, latent_dim, device=device)
        dummy_z2 = torch.randn(1, latent_dim, device=device)

        torch.onnx.export(
            self.ulep.predictor_head,
            (dummy_z1, dummy_z2),
            str(pred_path),
            input_names=["z_t_minus_1", "z_t_minus_2"],
            output_names=["z_hat_t"],
            dynamic_axes={"z_t_minus_1": {0: "batch"}, "z_t_minus_2": {0: "batch"}, "z_hat_t": {0: "batch"}},
            opset_version=17,
        )
        print(f"  Exported ULEP predictor: {pred_path}")

        # Export MR-GWD latent synth
        synth_path = out / "mrgwd_synth.onnx"
        dummy_z = torch.randn(1, latent_dim, device=device)

        torch.onnx.export(
            self.mrgwd.latent_synth.projector,
            (dummy_z,),
            str(synth_path),
            input_names=["z_t"],
            output_names=["vae_latent"],
            dynamic_axes={"z_t": {0: "batch"}, "vae_latent": {0: "batch"}},
            opset_version=17,
        )
        print(f"  Exported MR-GWD synth: {synth_path}")

        # Save config
        config = {
            "latent_dim": latent_dim,
            "feat_dim": 384,
            "output_size": list(self.mrgwd.output_size),
            "global_step": self.global_step,
        }
        with open(out / "config.json", "w") as f:
            json.dump(config, f, indent=2)
        print(f"  Saved ONNX config: {out / 'config.json'}")


# ================================================================
# Main Training Script
# ================================================================

def main():
    parser = argparse.ArgumentParser(description="OmniQuant-Apex Training")
    parser.add_argument("--data", type=str, required=True, help="Path to video data")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-epochs", type=int, default=5)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--latent-dim", type=int, default=512)
    parser.add_argument("--seq-len", type=int, default=4)
    parser.add_argument("--frame-size", type=int, nargs=2, default=[224, 224])
    parser.add_argument("--output-size", type=int, nargs=2, default=[256, 256])
    parser.add_argument("--output-dir", type=str, default="checkpoints")
    parser.add_argument("--onnx-dir", type=str, default="onnx_models")
    parser.add_argument("--no-amp", action="store_true")
    parser.add_argument("--eval-interval", type=int, default=5)
    parser.add_argument("--save-interval", type=int, default=10)
    parser.add_argument("--max-videos", type=int, default=-1)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)

    # DDP args
    parser.add_argument("--local-rank", type=int, default=-1)

    args = parser.parse_args()

    # Setup DDP
    is_distributed = args.local_rank != -1
    if is_distributed:
        dist.init_process_group(backend="nccl")
        rank = dist.get_rank()
        device = torch.device(f"cuda:{rank}")
        torch.cuda.set_device(device)
    else:
        rank = 0
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Reproducibility
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    if rank == 0:
        print("=" * 60)
        print("  OmniQuant-Apex Training Pipeline")
        print("=" * 60)
        print(f"  Device: {device}")
        print(f"  Distributed: {is_distributed}")
        print(f"  Batch size: {args.batch_size} (effective: {args.batch_size * args.gradient_accumulation})")
        print(f"  LR: {args.lr}, Warmup: {args.warmup_epochs} epochs")
        print(f"  Latent dim: {args.latent_dim}")
        print("=" * 60)

    # Build models
    ulep = ULEP(latent_dim=args.latent_dim, use_pretrained=False).to(device)
    mrgwd = MRGWD(
        latent_dim=args.latent_dim,
        output_size=tuple(args.output_size),
        use_vae=False,
    ).to(device)

    if is_distributed:
        ulep = DDP(ulep, device_ids=[rank], output_device=rank)
        mrgwd = DDP(mrgwd, device_ids=[rank], output_device=rank)

    # Dataset
    train_dataset = VideoFrameDataset(
        data_path=args.data,
        seq_len=args.seq_len,
        frame_size=tuple(args.frame_size),
        max_videos=args.max_videos,
    )
    val_dataset = VideoFrameDataset(
        data_path=args.data,
        seq_len=args.seq_len,
        frame_size=tuple(args.frame_size),
        max_videos=min(10, args.max_videos) if args.max_videos > 0 else 10,
    )

    if is_distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset, shuffle=False)
    else:
        train_sampler = None
        val_sampler = None

    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=(train_sampler is None),
        sampler=train_sampler,
        num_workers=args.workers,
        pin_memory=True,
        drop_last=True,
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        sampler=val_sampler,
        num_workers=args.workers,
        pin_memory=True,
    )

    if rank == 0:
        print(f"  Train samples: {len(train_dataset)}")
        print(f"  Val samples: {len(val_dataset)}")
        print(f"  Train batches: {len(train_loader)}")

    # Optimizer
    all_params = list(ulep.parameters()) + list(mrgwd.parameters())
    optimizer = AdamW(all_params, lr=args.lr, weight_decay=args.weight_decay)

    # LR scheduler: warmup + cosine decay
    warmup_scheduler = LinearLR(optimizer, start_factor=0.1, total_iters=args.warmup_epochs)
    cosine_scheduler = CosineAnnealingLR(optimizer, T_max=args.epochs - args.warmup_epochs)
    scheduler = SequentialLR(
        optimizer,
        schedulers=[warmup_scheduler, cosine_scheduler],
        milestones=[args.warmup_epochs],
    )

    # Loss
    loss_fn = OmniQuantLoss(
        lambda_recon=1.0,
        lambda_predict=0.5,
        lambda_percept=0.1,
        lambda_temporal=0.3,
        lambda_latent=0.01,
    ).to(device)

    # Engine
    engine = TrainingEngine(
        ulep=ulep,
        mrgwd=mrgwd,
        optimizer=optimizer,
        scheduler=scheduler,
        loss_fn=loss_fn,
        device=device,
        gradient_accumulation_steps=args.gradient_accumulation,
        use_amp=not args.no_amp,
        is_distributed=is_distributed,
        rank=rank,
        output_dir=args.output_dir,
    )

    # Training loop
    for epoch in range(args.epochs):
        engine.epoch = epoch

        if is_distributed:
            train_sampler.set_epoch(epoch)

        if rank == 0:
            print(f"\n{'='*60}")
            print(f"  Epoch {epoch+1}/{args.epochs}")
            print(f"{'='*60}")

        train_losses = engine.train_epoch(train_loader)

        if rank == 0:
            print(f"\n  Train Loss: {train_losses['total']:.4f}")
            print(f"    Recon: {train_losses['recon']:.4f}")
            print(f"    Predict: {train_losses['predict']:.4f}")
            print(f"    Percept: {train_losses['percept']:.4f}")
            print(f"    Temporal: {train_losses['temporal']:.4f}")
            print(f"    Latent: {train_losses['latent']:.4f}")

        # Validation
        if (epoch + 1) % args.eval_interval == 0:
            val_losses = engine.evaluate(val_loader)
            if rank == 0:
                print(f"\n  Val Loss: {val_losses['total']:.4f}")
                print(f"    Recon: {val_losses['recon']:.4f}")
                print(f"    Predict: {val_losses['predict']:.4f}")

        # Save checkpoint
        if (epoch + 1) % args.save_interval == 0:
            metrics = {"train_loss": train_losses["total"], "val_loss": val_losses.get("total", 0)}
            engine.save_checkpoint(f"epoch_{epoch+1}", metrics)

            if val_losses["total"] < engine.best_loss:
                engine.best_loss = val_losses["total"]
                engine.save_checkpoint("best", metrics)

    # Export ONNX
    if rank == 0:
        print("\n" + "=" * 60)
        print("  Exporting ONNX models...")
        engine.export_onnx(latent_dim=args.latent_dim, output_dir=args.onnx_dir)
        print("  Done!")


if __name__ == "__main__":
    main()
