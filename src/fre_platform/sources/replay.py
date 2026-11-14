"""Replay previously recorded IQ files (raw interleaved float32, ``.cf32``)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

from ..config import Settings
from .base import IQBlock, IQSource


class ReplaySource(IQSource):
    name = "replay"

    def __init__(self, cfg: Settings, path: str | os.PathLike | None = None, loop: bool = False):
        self.cfg = cfg
        p = Path(path or cfg.replay_path or "")
        self.files = sorted(p.glob("*.cf32")) if p.is_dir() else [p]
        if not self.files or not all(f.is_file() for f in self.files):
            raise FileNotFoundError(f"no .cf32 recordings found at {p}")
        self.loop = loop
        self.n = cfg.block_samples
        self._file_idx = 0
        self._data: np.ndarray | None = None
        self._pos = 0
        self._seq = 0
        self._t0_ns = time.time_ns()
        self._samples_total = 0

    def device_info(self) -> dict:
        return {"model": "replay", "receive_only": True, "files": [str(f) for f in self.files]}

    def _load_next(self) -> bool:
        if self._file_idx >= len(self.files):
            if not self.loop:
                return False
            self._file_idx = 0
        f = self.files[self._file_idx]
        self._file_idx += 1
        self._data = np.fromfile(f, dtype="<c8")
        self._pos = 0
        return True

    def read_block(self, timeout: float = 5.0) -> IQBlock | None:
        while self._data is None or self._pos + self.n > len(self._data):
            if not self._load_next():
                raise EOFError("replay finished")
        chunk = self._data[self._pos:self._pos + self.n].copy()
        self._pos += self.n
        ts_ns = self._t0_ns + int(self._samples_total * 1e9 / self.cfg.sample_rate_hz)
        self._samples_total += self.n
        block = IQBlock(samples=chunk, sample_rate_hz=self.cfg.sample_rate_hz,
                        center_freq_hz=self.cfg.center_freq_hz, ts_ns=ts_ns, seq=self._seq, source=self.name,
                        meta={"timestamp_method": "replay_sample_clock",
                              "file": str(self.files[self._file_idx - 1])})
        self._seq += 1
        return block
