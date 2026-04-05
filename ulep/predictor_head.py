"""
ULEP Temporal Predictor — GRU-based predictor for latent state evolution.

Given z_{t-1} and z_{t-2}, predicts ẑ_t using a gated recurrent architecture
with dual-input fusion. The smaller the residual Δz_t = z_t - ẑ_t,
the fewer bits needed to transmit it.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F


class PredictorHead(nn.Module):
    """
    Dual-input GRU-based temporal predictor.

    ẑ_t = f(z_{t-1}, z_{t-2})

    Architecture:
        1. Fuse z_{t-1} and z_{t-2} via a learned gating mechanism
        2. Pass through a GRU to capture temporal dynamics
        3. Project GRU hidden state → predicted ẑ_t on unit hypersphere
    """

    def __init__(self, latent_dim: int = 512, hidden_dim: int = 1024,
                 n_layers: int = 2, dropout: float = 0.1):
        super().__init__()
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim

        # Dual-input fusion gate
        self.gate_w1 = nn.Linear(latent_dim, latent_dim)
        self.gate_w2 = nn.Linear(latent_dim, latent_dim)
        self.gate_bias = nn.Parameter(torch.zeros(latent_dim))

        # Input projection
        self.input_proj = nn.Sequential(
            nn.Linear(latent_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
        )

        # GRU temporal model
        self.gru = nn.GRU(hidden_dim, hidden_dim, num_layers=n_layers,
                          batch_first=True, dropout=dropout if n_layers > 1 else 0.0)

        # Output projection
        self.output_proj = nn.Sequential(
            nn.Linear(hidden_dim, latent_dim * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(latent_dim * 2, latent_dim),
            nn.LayerNorm(latent_dim),
        )

    def fuse(self, z1: torch.Tensor, z2: torch.Tensor) -> torch.Tensor:
        """
        Learned gated fusion of two latent states.
        z1, z2: (B, D)  →  fused: (B, D)
        """
        # Gate: how much weight to give z1 vs z2
        gate = torch.sigmoid(self.gate_w1(z1) + self.gate_w2(z2) + self.gate_bias)
        return gate * z1 + (1 - gate) * z2

    def forward(self, z_t_minus_1: torch.Tensor,
                z_t_minus_2: torch.Tensor) -> torch.Tensor:
        """
        Predict ẑ_t from previous two latent states.

        z_t_minus_1: (B, D) or (D,)
        z_t_minus_2: (B, D) or (D,)
        returns:     (B, D) or (D,) - predicted latent state
        """
        squeeze = z_t_minus_1.dim() == 1
        if squeeze:
            z_t_minus_1 = z_t_minus_1.unsqueeze(0)
            z_t_minus_2 = z_t_minus_2.unsqueeze(0)

        B = z_t_minus_1.shape[0]

        # Fuse inputs
        fused = self.fuse(z_t_minus_1, z_t_minus_2)       # (B, D)

        # Project and run through time
        seq = self.input_proj(fused).unsqueeze(1)          # (B, 1, H)
        gru_out, _ = self.gru(seq)                         # (B, 1, H)
        h = gru_out.squeeze(1)                             # (B, H)

        # Project and normalise
        z_pred = self.output_proj(h)                       # (B, D)
        z_pred = F.normalize(z_pred, dim=-1)

        return z_pred.squeeze(0) if squeeze else z_pred

    def predict_with_residual_scaling(self,
                                      z_prev: torch.Tensor,
                                      z_prev2: torch.Tensor,
                                      scale: float = 1.0) -> torch.Tensor:
        """
        Predict with optional extrapolation scaling along the
        velocity vector (z_{t-1} - z_{t-2}) for smoother predictions.
        """
        z_pred_base = self.forward(z_prev, z_prev2)
        velocity = z_prev - z_prev2
        z_pred_extrap = F.normalize(z_pred_base + scale * velocity, dim=-1)
        return z_pred_extrap
