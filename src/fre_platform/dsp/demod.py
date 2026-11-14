"""Receive-only FSK demodulator for radio ripple-control telegrams.

Signal (BlinkenCity / 38C3 2024, SigID wiki): FSK with 170 Hz shift, 200 baud,
8E1 serial coding (8 data bits, even parity, 1 stop bit, LSB first), idle = mark.

Chain (all on already-received baseband samples, nothing transmitted):
  1. quadrature FSK discriminator  -> instantaneous frequency
  2. matched integrate per bit with Gardner-style timing recovery
  3. UART 8E1 framing: start bit, 8 data LSB-first, even parity, stop bit
  4. hand recovered bytes to the DIN 19244 framer / telegram decoder

A per-channel :class:`FskDemodulator` keeps state across blocks so telegrams
that straddle a block boundary are still recovered.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DEFAULT_SHIFT_HZ = 170.0
DEFAULT_BAUD = 200.0


@dataclass
class DemodStats:
    bytes_out: int = 0
    parity_errors: int = 0
    framing_errors: int = 0


class FskDemodulator:
    """Streaming 2-FSK + 8E1 UART demodulator for one channel's baseband."""

    FRAME_BITS = 11  # 1 start + 8 data + 1 parity + 1 stop

    def __init__(self, baseband_rate_hz: float, shift_hz: float = DEFAULT_SHIFT_HZ,
                 baud: float = DEFAULT_BAUD, invert: bool = True):
        self.fs = float(baseband_rate_hz)
        self.shift = float(shift_hz)
        self.baud = float(baud)
        self.sps = self.fs / self.baud                      # samples per symbol
        if self.sps < 4:
            raise ValueError(f"baseband rate {self.fs} Hz too low for {self.baud} Bd (need >= 4 sps)")
        # LSB: lower frequency = mark(1); set invert=False if mark is the upper tone
        self.invert = invert
        self._prev = np.complex64(0)
        self._buf = np.zeros(0, dtype=np.float64)           # pending instantaneous-frequency samples
        self.stats = DemodStats()

    # ------------------------------------------------------------------
    def _instantaneous_freq(self, x: np.ndarray) -> np.ndarray:
        """Quadrature discriminator -> frequency in Hz (one sample per input sample)."""
        xf = x.astype(np.complex64)
        prev = np.empty(len(xf) + 1, dtype=np.complex64)
        prev[0] = self._prev
        prev[1:] = xf
        self._prev = xf[-1] if len(xf) else self._prev
        d = xf * np.conj(prev[:-1])
        return np.angle(d) * self.fs / (2 * np.pi)

    def _is_mark(self, level: float) -> bool:
        # mark (bit 1) is the lower tone (negative frequency) when invert=True
        return (level < 0) == self.invert

    def _bit_at(self, f: np.ndarray, center: float) -> int:
        """Sample one bit by averaging a half-symbol window at ``center``."""
        half = self.sps / 4.0
        a = max(0, int(center - half))
        b = min(len(f), int(center + half) + 1)
        return 1 if self._is_mark(float(np.mean(f[a:b]))) else 0

    def process(self, baseband: np.ndarray) -> bytes:
        """Feed one block of complex baseband; return any UART 8E1 bytes recovered.

        Edge-triggered: each byte is framed from its own start-bit edge, so a
        constant timing offset (e.g. DDC filter group delay) does not corrupt
        data bits, and telegrams spanning blocks are reassembled via ``_buf``.
        """
        if len(baseband) == 0:
            return b""
        f = np.concatenate([self._buf, self._instantaneous_freq(baseband)])
        out = bytearray()
        sps = self.sps
        need = int(np.ceil(self.FRAME_BITS * sps)) + 2   # samples for one full frame
        i = 0
        n = len(f)
        while i <= n - need:
            if not self._is_mark(f[i]):                   # not idle: possible start-bit edge
                start = i
                # bit centres: start bit at +0.5 sps, data/parity/stop follow
                centres = start + (np.arange(self.FRAME_BITS) + 0.5) * sps
                if self._bit_at(f, centres[0]) != 0:      # start bit must be space(0); glitch -> skip
                    i += 1
                    continue
                bits = [self._bit_at(f, c) for c in centres]
                data_bits, parity, stop = bits[1:9], bits[9], bits[10]
                if stop != 1:
                    self.stats.framing_errors += 1
                    i += 1
                    continue
                if (sum(data_bits) + parity) & 1 != 0:    # even parity over data+parity
                    self.stats.parity_errors += 1
                    i += 1
                    continue
                value = 0
                for k, bit in enumerate(data_bits):
                    value |= (bit & 1) << k
                out.append(value)
                self.stats.bytes_out += 1
                i = int(round(start + self.FRAME_BITS * sps))
            else:
                i += 1
        self._buf = f[i:].copy()
        return bytes(out)

    def reset(self) -> None:
        self._prev = np.complex64(0)
        self._buf = np.zeros(0, dtype=np.float64)


def demodulate_to_bytes(baseband: np.ndarray, baseband_rate_hz: float, **kw) -> bytes:
    """One-shot helper for tests / offline files."""
    d = FskDemodulator(baseband_rate_hz, **kw)
    return d.process(baseband)
