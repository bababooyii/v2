"""
ULEP Encode Head — projects backbone patch features into a compact
object-centric latent representation z_t ∈ R^D.

Uses global average pooling + a learned attention pooling head to aggregate
patch representations into a single semantic summary vector.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class EncodeHead(nn.Module):
    """
    2-layer MLP + LayerNorm encode head with learned attention pooling.

    Takes patch features (B, N, feat_dim) and outputs z_t (B, latent_dim).
    """

    def __init__(self, feat_dim: int = 384, latent_dim: int = 512,
                 hidden_dim: int = 1024, dropout: float = 0.1):
        super().__init__()
        self.feat_dim = feat_dim
        self.latent_dim = latent_dim

        # Attention pooling: learn a query vector to pool patches
        self.attn_query = nn.Parameter(torch.randn(feat_dim))
        self.attn_proj = nn.Linear(feat_dim, feat_dim)

        # Projection MLP
        self.norm0 = nn.LayerNorm(feat_dim)
        self.fc1 = nn.Linear(feat_dim * 2, hidden_dim)   # concat avg + attn pool
        self.act = nn.GELU()
        self.drop = nn.Dropout(dropout)
        self.fc2 = nn.Linear(hidden_dim, latent_dim)
        self.norm_out = nn.LayerNorm(latent_dim)

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        """
        features: (B, N, feat_dim)
        returns:  (B, latent_dim) — normalised unit latent vector
        """
        B, N, C = features.shape
        features = self.norm0(features)

        # Global average pool
        avg_pool = features.mean(dim=1)  # (B, C)

        # Learned attention pool
        q = self.attn_query.unsqueeze(0).expand(B, -1)                    # (B, C)
        k = self.attn_proj(features)                                       # (B, N, C)
        scores = torch.bmm(k, q.unsqueeze(-1)).squeeze(-1) / (C ** 0.5)   # (B, N)
        weights = F.softmax(scores, dim=-1)                                 # (B, N)
        attn_pool = torch.bmm(weights.unsqueeze(1), features).squeeze(1)   # (B, C)

        # Concatenate and project
        combined = torch.cat([avg_pool, attn_pool], dim=-1)  # (B, 2C)
        z = self.fc2(self.drop(self.act(self.fc1(combined))))
        z = self.norm_out(z)

        # L2-normalise for semantic stability (unit hypersphere)
        z = F.normalize(z, dim=-1)
        return z
