import numpy as np
import pytest

from fre_platform.dsp.features import CHANNEL_FEATURES, FeatureExtractor, assess_quality
from fre_platform.sources.base import IQBlock
from fre_platform.sources.simulated import SimulatedSource


def _block(cfg, samples, ts=1_000_000_000):
    return IQBlock(samples=samples, sample_rate_hz=cfg.sample_rate_hz, center_freq_hz=cfg.center_freq_hz,
                   ts_ns=ts, seq=0, source="test")


def test_quality_good_block(cfg):
    b = SimulatedSource(cfg, seed=1).read_block()
    q = assess_quality(b, cfg.block_samples)
    assert q["valid"] and q["reasons"] == [] and q["clip_fraction"] == 0


@pytest.mark.parametrize("samples,reason", [
    (lambda n: np.zeros(n, np.complex64), "all-zero"),
    (lambda n: np.full(n, np.nan, np.complex64), "non-finite"),
    (lambda n: np.ones(n // 2, np.complex64), "length"),
])
def test_quality_invalid(cfg, samples, reason):
    q = assess_quality(_block(cfg, samples(cfg.block_samples)), cfg.block_samples)
    assert not q["valid"] and any(r.startswith(reason) for r in q["reasons"])


def test_quality_clipping_and_timing(cfg):
    x = np.full(cfg.block_samples, 0.995 + 0j, np.complex64)
    q = assess_quality(_block(cfg, x, ts=5), cfg.block_samples, prev_ts_ns=10)
    assert q["valid"]  # clipping is a warning, not fatal
    assert any("clipping" in r for r in q["reasons"]) and "non-monotonic timestamp" in q["reasons"]


def _mean_feats(cfg, scenario, channel_idx=1, n=6):
    fx = FeatureExtractor(cfg)
    src = SimulatedSource(cfg, scenario=scenario, seed=3)
    rows, bands = [], []
    for _ in range(n):
        b = src.read_block()
        band, psd = fx.band(b)
        rows.append(fx.channel(b, cfg.channels[channel_idx], band))
        bands.append(band)
    return ({k: float(np.mean([r[k] for r in rows])) for k in CHANNEL_FEATURES},
            {k: float(np.mean([r[k] for r in bands])) for k in bands[0]}, psd)


def test_normal_features(cfg):
    f, band, psd = _mean_feats(cfg, "normal")
    assert f["snr_db"] > 25 and abs(f["carrier_offset_hz"]) < 15 and f["unexpected_peaks"] < 2
    assert psd["n_bins"] == cfg.psd_bins and len(psd["db"]) == cfg.psd_bins


def test_reference_channel_detected(cfg):
    f, _, _ = _mean_feats(cfg, "normal", channel_idx=0)
    assert f["snr_db"] > 25 and f["fsk_activity"] < 0.05


def test_freq_offset_feature(cfg):
    f, _, _ = _mean_feats(cfg, "freq_offset")
    assert 25 < f["carrier_offset_hz"] < 55


def test_carrier_loss_feature(cfg):
    f, _, _ = _mean_feats(cfg, "carrier_loss")
    assert f["snr_db"] < 5


def test_wideband_jamming_raises_band_floor(cfg):
    _, normal, _ = _mean_feats(cfg, "normal")
    _, jam, _ = _mean_feats(cfg, "jamming_wideband")
    assert jam["band_noise_floor_db"] - normal["band_noise_floor_db"] > 20


def test_narrowband_jamming_signature(cfg):
    normal, _, _ = _mean_feats(cfg, "normal")
    f, _, _ = _mean_feats(cfg, "jamming_narrowband")
    # raised in-channel floor plus several stray spectral lines from the jammer
    assert f["noise_floor_db"] - normal["noise_floor_db"] > 10
    assert f["unexpected_peaks"] >= 3 > normal["unexpected_peaks"]


def test_flood_activity(cfg):
    f, _, _ = _mean_feats(cfg, "flood")
    assert f["fsk_activity"] > 0.85
