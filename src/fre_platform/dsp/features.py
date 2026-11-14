"""Feature extraction for receive-only long-wave observations.

For every IQ block we compute

* block quality checks (length, finite values, clipping, DC offset, timing),
* a band-wide power spectrum (compressed PSD for the raw dataset) and
  band statistics (noise floor, spectral flatness, total power),
* per-channel features after digital down-conversion to a narrow baseband:
  power, noise floor, SNR, carrier offset, FSK mark/space/centre energies,
  unexpected spectral peaks, activity and instantaneous-frequency statistics.

All powers are in dB relative to full scale (complex sample magnitude 1.0).
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy import signal, stats

from ..config import Channel, Settings
from ..sources.base import IQBlock

FSK_SHIFT_HZ = 170.0
SIGNAL_HALF_BW_HZ = 300.0
NOISE_BAND_HZ = (450.0, 900.0)  # upper edge (+200 Hz) bounds the floor-estimation passband
TONE_WIN_HZ = 30.0
PEAK_MATCH_HZ = 15.0
_EPS = 1e-20

CHANNEL_FEATURES = (
    "power_dbfs", "noise_floor_db", "snr_db", "carrier_offset_hz", "peak_offset_hz", "peak_db",
    "mark_rel_db", "space_rel_db", "center_rel_db", "fsk_index_db", "n_peaks", "unexpected_peaks",
    "active_fraction", "fsk_activity", "if_std_hz", "symbol_rate", "crest_factor_db", "amp_kurtosis",
    "floor_rel_band_db",
)
BAND_FEATURES = ("band_noise_floor_db", "band_flatness", "band_total_power_dbfs")


def _db(x):
    return 10.0 * np.log10(np.maximum(x, _EPS))


@lru_cache(maxsize=32)
def _lowpass_sos(fs: float, cutoff: float, order: int = 8):
    return signal.butter(order, cutoff, btype="low", fs=fs, output="sos")


@lru_cache(maxsize=32)
def _oscillator(n: int, f_off: float, fs: float) -> np.ndarray:
    return np.exp(-2j * np.pi * f_off * np.arange(n) / fs).astype(np.complex64)


def assess_quality(block: IQBlock, expected_samples: int, prev_ts_ns: int | None = None,
                   now_ns: int | None = None, ntp_synced: bool | None = None) -> dict:
    """Validate an IQ block. Returns a dict with ``valid`` and ``reasons``."""
    x = block.samples
    reasons: list[str] = []
    if x.dtype != np.complex64:
        reasons.append(f"unexpected dtype {x.dtype}")
    if len(x) != expected_samples:
        reasons.append(f"length {len(x)} != expected {expected_samples}")
    finite = bool(np.isfinite(x).all()) if len(x) else False
    if not finite:
        reasons.append("non-finite samples")
    rms = float(np.sqrt(np.mean(np.abs(x) ** 2))) if len(x) and finite else 0.0
    if finite and rms == 0.0:
        reasons.append("all-zero samples")
    clip = float(np.mean((np.abs(x.real) >= 0.99) | (np.abs(x.imag) >= 0.99))) if len(x) and finite else 0.0
    if clip > 1e-3:
        reasons.append(f"ADC clipping ({clip:.2%} of samples)")
    dc = complex(np.mean(x)) if len(x) and finite else 0j
    dc_rel_db = float(_db(abs(dc) ** 2) - _db(rms ** 2)) if rms > 0 else None
    if prev_ts_ns is not None and block.ts_ns <= prev_ts_ns:
        reasons.append("non-monotonic timestamp")
    if now_ns is not None and block.ts_ns > now_ns + 5_000_000_000:
        reasons.append("timestamp in the future")
    fatal = any(r.startswith(("unexpected dtype", "length", "non-finite", "all-zero")) for r in reasons)
    return {
        "valid": not fatal,
        "reasons": reasons,
        "rms_dbfs": round(float(_db(rms ** 2)), 3) if rms > 0 else None,
        "clip_fraction": round(clip, 6),
        "dc_rel_db": round(dc_rel_db, 2) if dc_rel_db is not None else None,
        "ntp_synced": ntp_synced,
        "dropped_blocks_before": block.dropped_blocks_before,
        "timestamp_reanchored": block.timestamp_reanchored,
    }


class FeatureExtractor:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.dec = int(cfg.sample_rate_hz // cfg.baseband_rate_hz)
        self.fs_bb = cfg.sample_rate_hz / self.dec
        self.sos = _lowpass_sos(float(cfg.sample_rate_hz), float(cfg.channel_cutoff_hz))

    # ---------------------------------------------------------------- band
    def band(self, block: IQBlock) -> tuple[dict, dict]:
        """Return (band_features, compressed_psd)."""
        x = block.samples
        nper = min(4096, len(x))
        f, p = signal.welch(x, fs=block.sample_rate_hz, nperseg=nper, return_onesided=False,
                            detrend=False, scaling="density")
        f = np.fft.fftshift(f)
        p = np.fft.fftshift(p)
        bins = min(self.cfg.psd_bins, len(p))
        usable = (len(p) // bins) * bins
        pc = p[:usable].reshape(bins, -1).mean(axis=1)
        step = (f[1] - f[0]) * (usable // bins)
        psd = {
            "f_start_hz": round(float(block.center_freq_hz + f[0]), 3),
            "f_step_hz": round(float(step), 6),
            "n_bins": int(bins),
            "db": [round(float(v), 2) for v in _db(pc)],
            "scaling": "dBFS/Hz",
        }
        flat = float(np.exp(np.mean(np.log(p + _EPS))) / (np.mean(p) + _EPS))
        feats = {
            "band_noise_floor_db": float(np.median(_db(p))),
            "band_flatness": flat,
            "band_total_power_dbfs": float(_db(np.mean(np.abs(x) ** 2))),
        }
        return feats, psd

    # ------------------------------------------------------------- channel
    def baseband(self, block: IQBlock, channel: Channel) -> np.ndarray:
        f_off = float(channel.freq_hz - block.center_freq_hz)
        y = block.samples * _oscillator(len(block.samples), f_off, float(block.sample_rate_hz))
        y = signal.sosfilt(self.sos, y)
        return y[:: self.dec]

    def channel(self, block: IQBlock, channel: Channel, band_feats: dict | None = None,
                baseband: np.ndarray | None = None) -> dict:
        y = self.baseband(block, channel) if baseband is None else baseband
        fs = self.fs_bb
        pw = np.abs(y) ** 2
        power = float(np.mean(pw))

        # Segment length adapts to the block so the Welch estimate always
        # averages ~7 segments (a single periodogram is far too noisy).
        nper = 1024
        while nper > 128 and len(y) / nper < 4:
            nper //= 2
        nper = min(nper, len(y))
        n_seg = max(1, (len(y) - nper) // (nper // 2) + 1)
        f, P = signal.welch(y, fs=fs, nperseg=nper, return_onesided=False, detrend=False, scaling="density")
        f = np.fft.fftshift(f)
        P = np.fft.fftshift(P)
        df = float(f[1] - f[0])
        Pdb = _db(P)
        af = np.abs(f)
        # Robust floor: low percentile over the filter passband (the signal and
        # its FSK sidebands occupy well under half of it), converted to an
        # unbiased noise-mean estimate using the chi-square (gamma) statistics of
        # an n_seg-average periodogram.
        pass_mask = af <= NOISE_BAND_HZ[1] + 200
        q20 = float(stats.gamma.ppf(0.2, n_seg) / n_seg)
        floor_density = float(np.percentile(P[pass_mask], 20)) / q20
        floor_db = float(_db(floor_density))
        sig_mask = af <= SIGNAL_HALF_BW_HZ
        band_power = float(P[sig_mask].sum() * df)
        noise_equiv = floor_density * (2 * SIGNAL_HALF_BW_HZ)
        snr = float(_db(max(band_power - noise_equiv, _EPS) / max(noise_equiv, _EPS)))

        excess = np.clip(P - floor_density, 0, None) * sig_mask
        tot = excess.sum()
        centroid = float((excess * f).sum() / tot) if tot > 0 else 0.0
        idx = int(np.argmax(np.where(sig_mask, P, 0)))
        peak_off = float(f[idx])
        if 0 < idx < len(P) - 1:  # parabolic interpolation on dB values
            a, b, c = Pdb[idx - 1], Pdb[idx], Pdb[idx + 1]
            den = a - 2 * b + c
            if den != 0:
                peak_off += 0.5 * (a - c) / den * df

        def tone_db(center: float) -> float:
            m = np.abs(f - center) <= TONE_WIN_HZ
            return float(_db(P[m].mean())) if m.any() else floor_db

        mark, space, ctr = tone_db(+FSK_SHIFT_HZ), tone_db(-FSK_SHIFT_HZ), tone_db(0.0)
        fsk_index = float(_db((10 ** (mark / 10) + 10 ** (space / 10)) / (2 * 10 ** (ctr / 10))))

        # Count only strong, well-defined spectral lines: within 15 dB of the
        # strongest one, >= 12 dB above the floor and >= 8 dB prominent.
        search = np.where(np.abs(f) <= 400, Pdb, -400.0)
        peaks, _ = signal.find_peaks(search, height=max(floor_db + 12, float(search.max()) - 15),
                                     prominence=8, distance=max(1, int(10 / df)))
        expected = np.array([-FSK_SHIFT_HZ, 0.0, FSK_SHIFT_HZ]) + centroid
        unexpected = sum(1 for pk in peaks if np.min(np.abs(f[pk] - expected)) > PEAK_MATCH_HZ)

        # time-domain statistics on samples well above the noise floor
        noise_power_td = floor_density * fs
        gate = pw[1:] > noise_power_td * 10.0
        inst = np.angle(y[1:] * np.conj(y[:-1])) * fs / (2 * np.pi)
        active = float(gate.mean()) if len(gate) else 0.0
        if gate.sum() > 10:
            g = inst[gate]
            if_std = float(np.std(g))
            dev = np.abs(g - centroid)
            fsk_act = float(np.mean((dev > 100) & (dev < 240))) * active
            sym = max(1, int(round(fs / 200.0)))
            usable = (len(inst) // sym) * sym
            sym_freq = inst[:usable].reshape(-1, sym).mean(axis=1)
            sym_gate = gate[:usable].reshape(-1, sym).mean(axis=1) > 0.8
            s = np.sign(sym_freq[sym_gate] - centroid)
            s = s[np.abs(sym_freq[sym_gate] - centroid) > 85]
            symbol_rate = float(np.count_nonzero(np.diff(s)) / max(block.duration_s, 1e-9))
        else:
            if_std, fsk_act, symbol_rate = 0.0, 0.0, 0.0

        amp = np.sqrt(pw)
        crest = float(_db(pw.max() / power)) if power > 0 else 0.0
        kurt = float(stats.kurtosis(amp)) if amp.std() > 0 else 0.0
        out = {
            "power_dbfs": float(_db(power)),
            "noise_floor_db": floor_db,
            "snr_db": snr,
            "carrier_offset_hz": centroid,
            "peak_offset_hz": peak_off,
            "peak_db": float(Pdb[idx]),
            "mark_rel_db": mark - floor_db,
            "space_rel_db": space - floor_db,
            "center_rel_db": ctr - floor_db,
            "fsk_index_db": fsk_index,
            "n_peaks": int(len(peaks)),
            "unexpected_peaks": int(unexpected),
            "active_fraction": active,
            "fsk_activity": fsk_act,
            "if_std_hz": if_std,
            "symbol_rate": symbol_rate,
            "crest_factor_db": crest,
            "amp_kurtosis": kurt,
            "floor_rel_band_db": (floor_db - band_feats["band_noise_floor_db"]) if band_feats else 0.0,
        }
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}


def feature_vector(feats: dict, band: dict, names=CHANNEL_FEATURES + BAND_FEATURES) -> np.ndarray:
    merged = {**band, **feats}
    v = np.array([float(merged.get(n, np.nan) if merged.get(n) is not None else np.nan) for n in names])
    return np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
