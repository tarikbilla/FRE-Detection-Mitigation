"""Synthetic long-wave band generator for authorized research and testing.

This is a *simplified model*, not the proprietary EFR telegram protocol: each
ripple-control channel is an unmodulated carrier that occasionally carries
binary FSK bursts (+/-170 Hz shift, 200 Bd); the DCF77 reference channel is an
AM carrier with second-marker amplitude reductions. Anomaly scenarios inject
the signal conditions a defender wants to detect (jamming, a second emitter,
frequency error, carrier loss, telegram flooding). Everything is generated in
memory; nothing is transmitted.
"""
from __future__ import annotations

import time

import numpy as np

from ..config import Settings
from .base import IQBlock, IQSource

FSK_SHIFT_HZ = 170.0
BAUD = 200.0
REFERENCE_CHANNELS = ("DCF77",)

SCENARIOS = ("normal", "jamming_wideband", "jamming_narrowband", "spoof", "carrier_loss", "freq_offset",
             "flood", "mixed")
LABELS = ("normal", "jamming_wideband", "jamming_narrowband", "spoof", "carrier_loss", "freq_offset", "flood")


class SimulatedSource(IQSource):
    name = "simulated"

    def __init__(self, cfg: Settings, scenario: str | None = None, seed: int | None = None,
                 realtime: bool = False, target_channel: str | None = None, max_blocks: int | None = None,
                 anomaly_probability: float = 0.35):
        scenario = scenario or cfg.sim_scenario
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {SCENARIOS}")
        self.cfg = cfg
        self.scenario = scenario
        self.realtime = realtime
        self.max_blocks = max_blocks
        self.anomaly_probability = anomaly_probability
        self.rng = np.random.default_rng(seed if seed is not None else cfg.sim_seed)
        self.fs = cfg.sample_rate_hz
        self.n = cfg.block_samples
        self.channels = list(cfg.channels)
        efr = [c.name for c in self.channels if c.name not in REFERENCE_CHANNELS]
        self.efr_channels = efr or [c.name for c in self.channels]
        self.target = target_channel or self.efr_channels[0]
        self.noise_amp = 1e-3
        self._phase = {c.name: float(self.rng.uniform(0, 2 * np.pi)) for c in self.channels}
        self._amp = {c.name: float(self.rng.uniform(0.01, 0.03)) for c in self.channels}
        self._rogue_phase = 0.0
        self._sample_index = 0
        self._seq = 0
        self._t0_ns = time.time_ns()
        self._next_deadline = time.monotonic()

    def set_scenario(self, scenario: str) -> None:
        """Switch scenario mid-stream (e.g. an attack starting after normal reception)."""
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; choose from {SCENARIOS}")
        self.scenario = scenario

    def device_info(self) -> dict:
        return {"model": "simulated", "receive_only": True, "scenario": self.scenario,
                "note": "synthetic signals for authorized research/testing"}

    # ------------------------------------------------------------------
    def _labels_for_block(self) -> dict[str, str]:
        labels = {c.name: "normal" for c in self.channels}
        sc = self.scenario
        if sc == "normal":
            return labels
        if sc == "jamming_wideband":
            return {k: "jamming_wideband" for k in labels}
        if sc == "mixed":
            if self.rng.random() >= self.anomaly_probability:
                return labels
            sc = str(self.rng.choice(LABELS[1:]))
            if sc == "jamming_wideband":
                return {k: "jamming_wideband" for k in labels}
            labels[str(self.rng.choice(self.efr_channels))] = sc
            return labels
        labels[self.target] = sc
        return labels

    def _fsk_freq(self, n: int, burst_prob: float, always: bool = False) -> np.ndarray:
        """Instantaneous frequency deviation (Hz) for one block: idle 0, FSK +/-shift in bursts."""
        dev = np.zeros(n, dtype=np.float64)
        if not always and self.rng.random() >= burst_prob:
            return dev
        sym_len = max(1, int(round(self.fs / BAUD)))
        if always:
            start, length = 0, n
        else:
            length = int(self.rng.uniform(0.3, 0.8) * n)
            start = int(self.rng.integers(0, max(1, n - length)))
        nsym = int(np.ceil(length / sym_len))
        bits = self.rng.integers(0, 2, nsym) * 2 - 1
        dev[start:start + length] = np.repeat(bits * FSK_SHIFT_HZ, sym_len)[:length]
        return dev

    def _tone(self, name: str, freq_offset: np.ndarray | float, amp: float | np.ndarray) -> np.ndarray:
        inst = 2 * np.pi * np.asarray(freq_offset, dtype=np.float64) / self.fs
        if np.ndim(inst) == 0:
            phase = self._phase[name] + inst * np.arange(1, self.n + 1)
        else:
            phase = self._phase[name] + np.cumsum(inst)
        self._phase[name] = float(np.mod(phase[-1], 2 * np.pi))
        return amp * np.exp(1j * phase)

    def generate(self) -> tuple[np.ndarray, dict[str, str]]:
        n, fs, rng = self.n, self.fs, self.rng
        labels = self._labels_for_block()
        x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * (self.noise_amp / np.sqrt(2))
        t_abs = (self._sample_index + np.arange(n)) / fs

        for ch in self.channels:
            label = labels[ch.name]
            base_off = ch.freq_hz - self.cfg.center_freq_hz
            # slow amplitude fading (random walk, bounded)
            self._amp[ch.name] = float(np.clip(self._amp[ch.name] * np.exp(rng.normal(0, 0.02)), 0.005, 0.05))
            amp = self._amp[ch.name]
            if label == "carrier_loss":
                # keep phase continuity bookkeeping but emit nothing
                self._phase[ch.name] = float(np.mod(self._phase[ch.name] + 2 * np.pi * base_off * n / fs, 2 * np.pi))
                continue
            if ch.name in REFERENCE_CHANNELS:
                frac = np.mod(t_abs, 1.0)
                sec = np.floor(t_abs).astype(np.int64)
                marker = np.where(sec % 2 == 0, 0.1, 0.2)  # 100 ms / 200 ms second markers
                env = np.where(frac < marker, 0.15, 1.0) * amp
                x += self._tone(ch.name, base_off, env)
                continue
            extra = 40.0 if label == "freq_offset" else 0.0
            dev = self._fsk_freq(n, burst_prob=0.25, always=(label == "flood"))
            x += self._tone(ch.name, base_off + extra + dev, amp)

            if label == "spoof":
                rogue_dev = self._fsk_freq(n, burst_prob=1.0, always=True)
                inst = 2 * np.pi * (base_off + 45.0 + rogue_dev) / fs
                ph = self._rogue_phase + np.cumsum(inst)
                self._rogue_phase = float(np.mod(ph[-1], 2 * np.pi))
                x += 1.5 * amp * np.exp(1j * ph)
            elif label == "jamming_narrowband":
                k = 40
                freqs = base_off + rng.uniform(-1000, 1000, k)
                phases = rng.uniform(0, 2 * np.pi, k)
                tt = np.arange(n) / fs
                jam = np.exp(1j * (2 * np.pi * np.outer(tt, freqs) + phases)).sum(axis=1)
                x += jam * (amp * 0.8)

        if any(v == "jamming_wideband" for v in labels.values()):
            x += (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * (self.noise_amp * 30 / np.sqrt(2))

        self._sample_index += n
        return x.astype(np.complex64), labels

    def read_block(self, timeout: float = 5.0) -> IQBlock | None:
        if self.max_blocks is not None and self._seq >= self.max_blocks:
            raise EOFError("simulation finished")
        if self.realtime:
            self._next_deadline += self.n / self.fs
            delay = self._next_deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        ts_ns = self._t0_ns + int(self._sample_index * 1e9 / self.fs)
        samples, labels = self.generate()
        block = IQBlock(samples=samples, sample_rate_hz=self.fs, center_freq_hz=self.cfg.center_freq_hz,
                        ts_ns=ts_ns, seq=self._seq, source=self.name, truth=labels,
                        meta={"timestamp_method": "simulated_sample_clock", "scenario": self.scenario})
        self._seq += 1
        return block
