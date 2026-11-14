"""Synthetic telegram signal generation for TESTING and SIMULATION ONLY.

These helpers build in-memory baseband samples and encode a *time* telegram so
the receive chain (demodulator -> framer -> decoder) can be verified without a
live signal. They are used by the simulator and the test-suite. Nothing here is
transmitted over any radio, and only the plaintext broadcast time telegram is
generated — no command/switching telegrams are crafted.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from .framing import START, END, checksum
from ..dsp.demod import DEFAULT_BAUD, DEFAULT_SHIFT_HZ


def build_time_frame(ts: datetime) -> bytes:
    """Build the bytes of a DIN 19244 broadcast *time* telegram (address 0x0000).

    Inverse of :func:`fre_platform.protocol.telegram.decode_time_payload`.
    """
    payload = bytes([
        0x00,
        (ts.second << 2) & 0xFF,
        ts.minute & 0xFF,
        ts.hour & 0x7F,  # DST bit (0x80) left off in synthetic data
        (((ts.weekday() + 1) % 7 & 0x7) << 5) | (ts.day & 0x1F),  # Python Mon=0 -> EFR Sun=0
        ts.month & 0xFF,
        (ts.year - 2000) & 0xFF,
    ])
    control = 0x57
    address = b"\x00\x00"
    user = bytes([control]) + address + payload
    return bytes([START, len(user), len(user), START]) + user + bytes([checksum(user), END])


def bytes_to_8e1_bits(data: bytes) -> list[int]:
    """Encode bytes as 8E1 UART bits (idle=1, start=0, 8 data LSB-first, even parity, stop=1)."""
    bits = [1] * 8  # idle/preamble
    for byte in data:
        bits.append(0)  # start
        ones = 0
        for k in range(8):
            b = (byte >> k) & 1
            bits.append(b)
            ones += b
        bits.append(ones & 1)  # even parity
        bits.append(1)         # stop
    bits += [1] * 4            # idle tail
    return bits


def fsk_baseband(bits: list[int], fs: float, baud: float = DEFAULT_BAUD, shift_hz: float = DEFAULT_SHIFT_HZ,
                 amplitude: float = 1.0, invert: bool = True) -> np.ndarray:
    """Modulate a bitstream to complex baseband 2-FSK (mark=1 -> lower tone if invert)."""
    sps = int(round(fs / baud))
    freqs = np.empty(len(bits) * sps)
    for i, bit in enumerate(bits):
        mark = bit == 1
        f = -shift_hz if (mark == invert) else shift_hz
        freqs[i * sps:(i + 1) * sps] = f
    phase = 2 * np.pi * np.cumsum(freqs) / fs
    return (amplitude * np.exp(1j * phase)).astype(np.complex64)


def time_telegram_baseband(ts: datetime, fs: float, **kw) -> tuple[np.ndarray, bytes]:
    """Return (baseband samples, frame bytes) for a time telegram at ``ts``."""
    frame = build_time_frame(ts)
    bb = fsk_baseband(bytes_to_8e1_bits(frame), fs, **kw)
    return bb, frame
