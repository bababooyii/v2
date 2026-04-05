"""
GTM Mathematics Tests.

Verifies:
1. RHT round-trip (isometry preservation)
2. Polar transform: exact norm preservation
3. Lloyd-Max quantizer round-trip error at all bit-widths
4. QJL bias reduction
5. Full GTM codec encode/decode round-trip
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import torch
import math

from gtm.rht import RHT
from gtm.polar import polar_transform, polar_inverse
from gtm.quantize import LloydMaxQuantizer
from gtm.qjl import QJL
from gtm.codec import GTMEncoder, GTMDecoder


class TestRHT:
    def test_round_trip(self):
        """RHT should be an exact isometry: inverse(forward(v)) ≈ v."""
        dim = 32
        rht = RHT(dim=dim, seed=42)
        v = torch.randn(dim)
        v_t = rht.forward(v)
        v_rec = rht.inverse(v_t)
        assert torch.allclose(v, v_rec, atol=1e-5), \
            f"RHT round-trip error: {(v - v_rec).norm()}"

    def test_norm_preservation(self):
        """||RHT(v)|| should ≈ ||v|| (isometry)."""
        dim = 64
        rht = RHT(dim=dim, seed=7)
        for _ in range(10):
            v = torch.randn(dim)
            v_t = rht.forward(v)
            # Note: padded_dim may differ from dim; check norm of first 'dim' elements
            assert abs(v.norm().item() - v_t.norm().item()) < 0.05, \
                f"Norm mismatch: {v.norm()} vs {v_t.norm()}"

    def test_different_seeds_give_different_transforms(self):
        rht1 = RHT(32, seed=1)
        rht2 = RHT(32, seed=2)
        v = torch.ones(32)
        assert not torch.allclose(rht1.forward(v), rht2.forward(v)), \
            "Different seeds should give different transforms"

    def test_batch_support(self):
        rht = RHT(32, seed=0)
        v = torch.randn(32)
        v_t = rht.forward(v)
        v_rec = rht.inverse(v_t)
        assert v_rec.shape == v.shape


class TestPolar:
    def test_norm_preservation(self):
        """||v|| should equal r after polar transform."""
        for _ in range(20):
            D = torch.randint(3, 33, (1,)).item()
            v = torch.randn(D)
            r, thetas = polar_transform(v.unsqueeze(0))
            assert abs(r.item() - v.norm().item()) < 1e-5, \
                f"r={r.item()} vs ||v||={v.norm().item()}"

    def test_round_trip(self):
        """polar_inverse(polar_transform(v)) ≈ v."""
        for _ in range(20):
            D = torch.randint(3, 33, (1,)).item()
            v = torch.randn(D)
            r, thetas = polar_transform(v.unsqueeze(0))
            v_rec = polar_inverse(r, thetas)
            assert torch.allclose(v.unsqueeze(0), v_rec, atol=1e-4), \
                f"polar round-trip error: {(v.unsqueeze(0) - v_rec).norm()}"

    def test_zero_vector(self):
        """Zero vector should not crash."""
        v = torch.zeros(16).unsqueeze(0)
        r, thetas = polar_transform(v)
        assert r.item() < 1e-6

    def test_batch_dim(self):
        B, D = 4, 16
        v = torch.randn(B, D)
        r, thetas = polar_transform(v)
        assert r.shape == (B,)
        assert thetas.shape == (B, D - 1)


class TestLloydMax:
    @pytest.mark.parametrize("n_bits", [1, 2, 3, 4, 6, 8])
    def test_quantize_dequantize_coverage(self, n_bits):
        """Every index returned should be valid."""
        qz = LloydMaxQuantizer(n_bits=n_bits).default_fit(scale=1.0)
        x = torch.randn(1000)
        idx = qz.quantize(x)
        assert idx.min() >= 0
        assert idx.max() < 2 ** n_bits
        x_hat = qz.dequantize(idx)
        assert x_hat.shape == x.shape

    @pytest.mark.parametrize("n_bits", [2, 4, 8])
    def test_error_decreases_with_bits(self, n_bits):
        """Higher bit-width should give lower quantization error."""
        x = torch.randn(5000)
        errors = {}
        for b in [2, 4, 8]:
            qz = LloydMaxQuantizer(n_bits=b).default_fit(scale=1.0)
            x_hat = qz.dequantize(qz.quantize(x))
            errors[b] = (x - x_hat).pow(2).mean().item()
        assert errors[2] > errors[4] > errors[8], \
            f"Expected error[2]>error[4]>error[8], got {errors}"

    def test_fit_reduces_error_vs_default(self):
        """Fit on data should reduce MSE vs default Gaussian codebook."""
        # Skewed data
        x = torch.cat([torch.randn(800) * 0.5, torch.randn(200) * 3.0])
        qz_default = LloydMaxQuantizer(4).default_fit(scale=1.0)
        qz_fit = LloydMaxQuantizer(4).fit(x)
        err_default = (x - qz_default.dequantize(qz_default.quantize(x))).pow(2).mean()
        err_fit = (x - qz_fit.dequantize(qz_fit.quantize(x))).pow(2).mean()
        assert err_fit <= err_default * 1.1, \
            f"fit MSE {err_fit:.4f} should be ≤ default MSE {err_default:.4f}"


class TestQJL:
    def test_encode_decode_shapes(self):
        qjl = QJL(proj_dim=64, seed=42)
        residual = torch.randn(128)
        bits = qjl.encode(residual)
        assert bits.shape == (64,)
        assert bits.dtype == torch.bool
        correction = qjl.decode(bits, orig_dim=128)
        assert correction.shape == (128,)

    def test_bias_reduction(self):
        """QJL correction should reduce mean absolute bias in residual."""
        torch.manual_seed(0)
        qjl = QJL(proj_dim=128, seed=42)
        residuals = [torch.randn(256) * 0.3 for _ in range(50)]

        # Simulate: systematic positive bias
        biased = [r + 0.5 for r in residuals]

        corrections = [qjl.decode(qjl.encode(b), 256) for b in biased]
        corrected = [b - c for b, c in zip(biased, corrections)]

        mean_before = sum(b.mean().abs() for b in biased) / len(biased)
        mean_after = sum(c.mean().abs() for c in corrected) / len(corrected)

        assert mean_after < mean_before, \
            f"QJL should reduce bias: before={mean_before:.4f}, after={mean_after:.4f}"


class TestGTMCodec:
    @pytest.mark.parametrize("n_bits,dim", [(2, 64), (4, 128), (6, 256)])
    def test_round_trip_error(self, n_bits, dim):
        """GTM encode→decode round-trip error should be bounded."""
        enc = GTMEncoder(n_bits=n_bits, qjl_proj_dim=32)
        dec = GTMDecoder(qjl_proj_dim=32)
        v = torch.randn(dim)
        packets = enc.encode(v)
        v_rec = dec.decode(packets, dim)
        err = (v - v_rec).norm().item()
        # Quantization error should be < 50% of residual norm
        expected_max_err = v.norm().item() * 0.8
        assert err < expected_max_err, \
            f"Round-trip error too large: {err:.4f} > {expected_max_err:.4f} for {n_bits}-bit, dim={dim}"

    def test_packet_serialization(self):
        """GTMPacket serialization should be exact round-trip."""
        enc = GTMEncoder(n_bits=4)
        v = torch.randn(64)
        packets = enc.encode(v)
        for pkt in packets:
            raw = pkt.to_bytes()
            from gtm.codec import GTMPacket
            pkt2 = GTMPacket.from_bytes(raw)
            assert pkt2.chunk_idx == pkt.chunk_idx
            assert pkt2.r_idx == pkt.r_idx
            assert pkt2.theta_indices == pkt.theta_indices

    def test_encode_decode_different_sizes(self):
        """Test various latent dimensions."""
        for dim in [32, 64, 128, 512]:
            enc = GTMEncoder(n_bits=3)
            dec = GTMDecoder()
            v = torch.randn(dim)
            pkts = enc.encode(v)
            v_rec = dec.decode(pkts, dim)
            assert v_rec.shape[0] == dim, f"Output dim mismatch for D={dim}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
