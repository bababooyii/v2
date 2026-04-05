"""
Download pre-trained weights and export to ONNX for Rust inference.

Uses ONLY torch.nn + huggingface_hub + safetensors.
No torchvision, transformers, or diffusers imports — avoids Python 3.14 compat issues.

Usage:
  python train/download_pretrained.py --output onnx_models
"""
import os
import sys
import json
import argparse
import torch
import torch.nn as nn
import math
import numpy as np
from pathlib import Path


def build_dinov2_small_vit(config):
    """Build a minimal DINOv2 ViT-S/14 model using only torch.nn."""
    embed_dim = config.get("hidden_size", 384)
    num_heads = config.get("num_attention_heads", 6)
    num_layers = config.get("num_hidden_layers", 12)
    patch_size = config.get("patch_size", 14)
    # Use 224x224 for ONNX export (DINOv2 supports 224, 518, etc.)
    image_size = 224
    num_patches = (image_size // patch_size) ** 2

    class PatchEmbed(nn.Module):
        def __init__(self):
            super().__init__()
            self.proj = nn.Conv2d(3, embed_dim, kernel_size=patch_size, stride=patch_size)
            self.norm = nn.LayerNorm(embed_dim)

        def forward(self, x):
            x = self.proj(x)  # (B, D, H/P, W/P)
            x = x.flatten(2).transpose(1, 2)  # (B, N, D)
            return self.norm(x)

    class Attention(nn.Module):
        def __init__(self):
            super().__init__()
            self.num_heads = num_heads
            self.head_dim = embed_dim // num_heads
            self.scale = self.head_dim ** -0.5
            self.qkv = nn.Linear(embed_dim, embed_dim * 3)
            self.proj = nn.Linear(embed_dim, embed_dim)

        def forward(self, x):
            B, N, C = x.shape
            qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
            q, k, v = qkv.unbind(0)
            attn = (q @ k.transpose(-2, -1)) * self.scale
            attn = attn.softmax(dim=-1)
            x = (attn @ v).transpose(1, 2).reshape(B, N, C)
            return self.proj(x)

    class Mlp(nn.Module):
        def __init__(self):
            super().__init__()
            mlp_ratio = config.get("mlp_ratio", 4.0)
            hidden_features = int(embed_dim * mlp_ratio)
            self.fc1 = nn.Linear(embed_dim, hidden_features)
            self.act = nn.GELU()
            self.fc2 = nn.Linear(hidden_features, embed_dim)

        def forward(self, x):
            return self.fc2(self.act(self.fc1(x)))

    class Block(nn.Module):
        def __init__(self):
            super().__init__()
            self.norm1 = nn.LayerNorm(embed_dim)
            self.attn = Attention()
            self.norm2 = nn.LayerNorm(embed_dim)
            self.mlp = Mlp()

        def forward(self, x):
            x = x + self.attn(self.norm1(x))
            x = x + self.mlp(self.norm2(x))
            return x

    class DinoViT(nn.Module):
        def __init__(self):
            super().__init__()
            self.patch_embed = PatchEmbed()
            self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
            self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))
            self.blocks = nn.ModuleList([Block() for _ in range(num_layers)])
            self.norm = nn.LayerNorm(embed_dim)

        def forward(self, pixel_values):
            B = pixel_values.shape[0]
            x = self.patch_embed(pixel_values)
            cls_tokens = self.cls_token.expand(B, -1, -1)
            x = torch.cat([cls_tokens, x], dim=1)
            x = x + self.pos_embed
            for blk in self.blocks:
                x = blk(x)
            return self.norm(x)

    return DinoViT()


def download_dinov2_small(output_dir: Path):
    """Download DINOv2-small and export to ONNX using only torch.nn."""
    print("[1/4] Downloading DINOv2-small backbone...")

    from huggingface_hub import hf_hub_download, snapshot_download
    from safetensors.torch import load_file

    print("  Downloading from HuggingFace (may take a minute)...")
    model_dir = snapshot_download(
        "facebook/dinov2-small",
        ignore_patterns=["*.msgpack", "*.h5", "*.pb", "*.onnx"],
        local_dir=output_dir / "_dinov2_cache",
    )

    # Load config
    with open(os.path.join(model_dir, "config.json")) as f:
        model_config = json.load(f)

    print("  Building model architecture...")
    model = build_dinov2_small_vit(model_config)

    # Load weights
    st_file = os.path.join(model_dir, "model.safetensors")
    if os.path.exists(st_file):
        print("  Loading pretrained weights...")
        state_dict = load_file(st_file)
        # Map DINOv2 keys to our architecture
        mapped = {}
        for k, v in state_dict.items():
            nk = k
            # patch_embed
            nk = nk.replace("patch_embed.projection", "patch_embed.proj")
            # blocks.X -> blocks.X
            nk = nk.replace(".attn.qkv", ".attn.qkv")
            nk = nk.replace(".attn.proj", ".attn.proj")
            nk = nk.replace(".mlp.fc1", ".mlp.fc1")
            nk = nk.replace(".mlp.fc2", ".mlp.fc2")
            # norm
            nk = nk.replace("norm.", "norm.")
            mapped[nk] = v

        missing, unexpected = model.load_state_dict(mapped, strict=False)
        if missing:
            print(f"  Warning: {len(missing)} keys missing (using random init)")
        if unexpected:
            print(f"  Warning: {len(unexpected)} unexpected keys (ignored)")
        print(f"  ✓ Loaded pretrained weights")
    else:
        print("  Warning: No safetensors found, using random initialization")

    model.eval()

    # Export to ONNX
    onnx_path = output_dir / "dinov2_small.onnx"
    dummy_input = torch.randn(1, 3, 224, 224)

    print(f"  Exporting to {onnx_path}...")
    torch.onnx.export(
        model,
        dummy_input,
        str(onnx_path),
        input_names=["pixel_values"],
        output_names=["last_hidden_state"],
        dynamic_axes={
            "pixel_values": {0: "batch"},
            "last_hidden_state": {0: "batch", 1: "n_tokens"},
        },
        opset_version=17,
    )
    size_mb = onnx_path.stat().st_size / 1e6
    print(f"  ✓ DINOv2-small exported ({size_mb:.1f} MB)")

    # Clean up cache
    import shutil
    cache_dir = output_dir / "_dinov2_cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


def build_sd_vae_decoder():
    """Build SD-VAE decoder using only torch.nn."""
    class Upsample2D(nn.Module):
        def __init__(self, channels):
            super().__init__()
            self.conv = nn.Conv2d(channels, channels, 3, padding=1)

        def forward(self, x):
            x = nn.functional.interpolate(x, scale_factor=2.0, mode="nearest")
            return self.conv(x)

    class ResnetBlock2D(nn.Module):
        def __init__(self, in_channels, out_channels):
            super().__init__()
            self.norm1 = nn.GroupNorm(32, in_channels, eps=1e-6)
            self.conv1 = nn.Conv2d(in_channels, out_channels, 3, padding=1)
            self.norm2 = nn.GroupNorm(32, out_channels, eps=1e-6)
            self.conv2 = nn.Conv2d(out_channels, out_channels, 3, padding=1)
            self.nin_shortcut = nn.Conv2d(in_channels, out_channels, 1) if in_channels != out_channels else nn.Identity()

        def forward(self, x):
            h = self.conv1(nn.functional.silu(self.norm1(x)))
            h = self.conv2(nn.functional.silu(self.norm2(h)))
            return self.nin_shortcut(x) + h

    class MidBlock(nn.Module):
        def __init__(self, channels):
            super().__init__()
            self.res_1 = ResnetBlock2D(channels, channels)
            self.res_2 = ResnetBlock2D(channels, channels)

        def forward(self, x):
            x = self.res_1(x)
            x = self.res_2(x)
            return x

    class Decoder(nn.Module):
        def __init__(self):
            super().__init__()
            self.conv_in = nn.Conv2d(4, 512, 3, padding=1)
            self.mid = MidBlock(512)
            self.up_blocks = nn.ModuleList([
                nn.ModuleList([
                    ResnetBlock2D(512, 512),
                    ResnetBlock2D(512, 512),
                    ResnetBlock2D(512, 512),
                    Upsample2D(512),
                ]),
                nn.ModuleList([
                    ResnetBlock2D(512, 512),
                    ResnetBlock2D(512, 512),
                    ResnetBlock2D(512, 512),
                    Upsample2D(512),
                ]),
                nn.ModuleList([
                    ResnetBlock2D(512, 256),
                    ResnetBlock2D(256, 256),
                    ResnetBlock2D(256, 256),
                    Upsample2D(256),
                ]),
                nn.ModuleList([
                    ResnetBlock2D(256, 128),
                    ResnetBlock2D(128, 128),
                    ResnetBlock2D(128, 128),
                ]),
            ])
            self.conv_norm_out = nn.GroupNorm(32, 128, eps=1e-6)
            self.conv_out = nn.Conv2d(128, 3, 3, padding=1)

        def forward(self, z):
            x = self.conv_in(z)
            x = self.mid(x)
            for block in self.up_blocks:
                for layer in block:
                    x = layer(x)
            x = self.conv_out(nn.functional.silu(self.conv_norm_out(x)))
            return x

    return Decoder()


def download_sd_vae(output_dir: Path):
    """Download SD-VAE-ft-mse decoder and export to ONNX."""
    print("[2/4] Downloading SD-VAE-ft-mse decoder...")

    from huggingface_hub import snapshot_download
    from safetensors.torch import load_file

    print("  Downloading from HuggingFace...")
    model_dir = snapshot_download(
        "stabilityai/sd-vae-ft-mse",
        ignore_patterns=["*.msgpack", "*.h5", "*.pb", "*.onnx", "*.ckpt"],
        local_dir=output_dir / "_vae_cache",
    )

    model = build_sd_vae_decoder()

    # Try to load weights
    st_file = os.path.join(model_dir, "diffusion_pytorch_model.safetensors")
    if os.path.exists(st_file):
        print("  Loading pretrained weights...")
        state_dict = load_file(st_file)
        # Map decoder keys
        mapped = {}
        for k, v in state_dict.items():
            if k.startswith("decoder."):
                nk = k[len("decoder."):]
                mapped[nk] = v
            else:
                mapped[k] = v

        missing, unexpected = model.load_state_dict(mapped, strict=False)
        if missing:
            print(f"  Warning: {len(missing)} keys missing")
        print(f"  ✓ Loaded pretrained weights")
    else:
        print("  Warning: No weights found, using random initialization")

    model.eval()

    onnx_path = output_dir / "sd_vae_decoder.onnx"
    dummy_latent = torch.randn(1, 4, 32, 32)

    print(f"  Exporting to {onnx_path}...")
    torch.onnx.export(
        model,
        dummy_latent,
        str(onnx_path),
        input_names=["latent"],
        output_names=["sample"],
        dynamic_axes={
            "latent": {0: "batch"},
            "sample": {0: "batch"},
        },
        opset_version=17,
    )
    size_mb = onnx_path.stat().st_size / 1e6
    print(f"  ✓ SD-VAE decoder exported ({size_mb:.1f} MB)")

    import shutil
    cache_dir = output_dir / "_vae_cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)


def export_ulep_heads(output_dir: Path, latent_dim: int = 512):
    """Export ULEP encode head and predictor head to ONNX."""
    print("[3/4] Exporting ULEP heads (encode + predictor)...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ulep.model import ULEP

    ulep = ULEP(latent_dim=latent_dim, use_pretrained=False)
    ulep.eval()

    dummy_features = torch.randn(1, 196, 384)
    encode_path = output_dir / "ulep_encode_head.onnx"

    torch.onnx.export(
        ulep.encode_head,
        dummy_features,
        str(encode_path),
        input_names=["features"],
        output_names=["z_t"],
        dynamic_axes={
            "features": {0: "batch", 1: "n_patches"},
            "z_t": {0: "batch"},
        },
        opset_version=17,
    )
    size_mb = encode_path.stat().st_size / 1e6
    print(f"  ✓ Encode head exported ({size_mb:.1f} MB)")

    dummy_z1 = torch.randn(1, latent_dim)
    dummy_z2 = torch.randn(1, latent_dim)
    pred_path = output_dir / "ulep_predictor.onnx"

    torch.onnx.export(
        ulep.predictor_head,
        (dummy_z1, dummy_z2),
        str(pred_path),
        input_names=["z_t_minus_1", "z_t_minus_2"],
        output_names=["z_hat_t"],
        dynamic_axes={
            "z_t_minus_1": {0: "batch"},
            "z_t_minus_2": {0: "batch"},
            "z_hat_t": {0: "batch"},
        },
        opset_version=17,
    )
    size_mb = pred_path.stat().st_size / 1e6
    print(f"  ✓ Predictor head exported ({size_mb:.1f} MB)")


def export_mrgwd_projector(output_dir: Path, latent_dim: int = 512):
    """Export MR-GWD latent projector to ONNX."""
    print("[4/4] Exporting MR-GWD projector...")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from mrgwd.latent_diffusion import LatentProjector

    projector = LatentProjector(latent_dim=latent_dim)
    projector.eval()

    dummy_z = torch.randn(1, latent_dim)
    proj_path = output_dir / "mrgwd_projector.onnx"

    torch.onnx.export(
        projector,
        dummy_z,
        str(proj_path),
        input_names=["z_t"],
        output_names=["vae_latent"],
        dynamic_axes={
            "z_t": {0: "batch"},
            "vae_latent": {0: "batch"},
        },
        opset_version=17,
    )
    size_mb = proj_path.stat().st_size / 1e6
    print(f"  ✓ MR-GWD projector exported ({size_mb:.1f} MB)")


def save_config(output_dir: Path, latent_dim: int = 512):
    """Save model config for Rust engine."""
    config = {
        "latent_dim": latent_dim,
        "feat_dim": 384,
        "output_size": [256, 256],
        "models": {
            "dinov2_backbone": "dinov2_small.onnx",
            "ulep_encode_head": "ulep_encode_head.onnx",
            "ulep_predictor": "ulep_predictor.onnx",
            "sd_vae_decoder": "sd_vae_decoder.onnx",
            "mrgwd_projector": "mrgwd_projector.onnx",
        },
        "pretrained": True,
        "source": "facebook/dinov2-small + stabilityai/sd-vae-ft-mse",
    }

    config_path = output_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    print(f"\n✓ Config saved to {config_path}")


def main():
    parser = argparse.ArgumentParser(description="Download pre-trained weights and export to ONNX")
    parser.add_argument("--output", type=str, default="onnx_models", help="Output directory")
    parser.add_argument("--latent-dim", type=int, default=512, help="Latent dimension")
    parser.add_argument("--skip-dinov2", action="store_true", help="Skip DINOv2 download")
    parser.add_argument("--skip-vae", action="store_true", help="Skip SD-VAE download")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  OmniQuant-Apex: Pre-trained Model Download")
    print("=" * 60)
    print()

    if not args.skip_dinov2:
        download_dinov2_small(output_dir)

    if not args.skip_vae:
        download_sd_vae(output_dir)

    export_ulep_heads(output_dir, args.latent_dim)
    export_mrgwd_projector(output_dir, args.latent_dim)
    save_config(output_dir, args.latent_dim)

    # Summary
    print("\n" + "=" * 60)
    print("  Export Summary:")
    print("=" * 60)
    total_size = 0
    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.suffix in (".onnx", ".json"):
            size = f.stat().st_size
            total_size += size
            print(f"  {f.name:<35} {size/1e6:>8.1f} MB")
    print(f"  {'TOTAL':<35} {total_size/1e6:>8.1f} MB")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Models exported to:", output_dir.absolute())
    print("  2. Fine-tune on Google Colab: train/finetune_colab.ipynb")
    print("  3. Load in Rust engine with --onnx-models flag")


if __name__ == "__main__":
    main()
