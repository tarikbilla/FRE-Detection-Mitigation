"""DIN 19244 / IEC 60870-5 FT1.2 variable-length frame parsing (receive-only).

Radio ripple-control (EFR/FRE) telegrams use the FT1.2 variable frame:

    0x68  L  L  0x68  C  A...  <user data>  CS  0x16
    |     |  |  |     |  |     |            |    end
    |     |  |  |     |  |     |            checksum = sum(C..end of data) & 0xFF
    |     |  |  |     control + address + payload  (L bytes total)
    |     length L repeated, then start byte repeated
    start byte

This module only *parses* bytes that were received and demodulated; it never
builds or transmits telegrams. Reference (educational): BlinkenCity, 38C3 2024;
signal spec FSK-LSB 170 Hz shift / 200 Bd / 8E1.
"""
from __future__ import annotations

from dataclasses import dataclass, field

START = 0x68
END = 0x16


@dataclass
class Frame:
    control: int
    user_data: bytes          # C + A + payload, exactly L bytes
    checksum_ok: bool
    raw: bytes                # full frame including 68..16
    start_offset: int

    @property
    def length(self) -> int:
        return len(self.user_data)

    @property
    def address(self) -> int:
        # 16-bit EVU address follows the control byte (little data after C)
        return int.from_bytes(self.user_data[1:3], "big") if len(self.user_data) >= 3 else -1

    @property
    def payload(self) -> bytes:
        return self.user_data[3:]

    def hex(self) -> str:
        return self.raw.hex()


def checksum(user_data: bytes) -> int:
    """FT1.2 checksum: arithmetic sum of all user-data bytes (C..payload) mod 256."""
    return sum(user_data) & 0xFF


def parse_stream(buffer: bytes, require_checksum: bool = True, max_keep: int = 4096) -> tuple[list[Frame], bytes]:
    """Parse ``buffer`` and return (complete frames, bytes to keep for next call).

    For streaming demodulation where a frame may straddle block boundaries: the
    returned remainder is everything after the last complete frame (a possible
    partial frame), trimmed to ``max_keep`` so unframed noise cannot grow without
    bound.
    """
    frames = parse_frames(buffer, require_checksum=require_checksum)
    if frames:
        last = frames[-1]
        remainder = buffer[last.start_offset + len(last.raw):]
    else:
        remainder = buffer
    if len(remainder) > max_keep:
        remainder = remainder[-max_keep:]
    return frames, remainder


def parse_frames(data: bytes, require_checksum: bool = True) -> list[Frame]:
    """Find all FT1.2 variable frames in ``data``.

    Resynchronises on every ``0x68``, so a corrupt byte only costs one frame.
    With ``require_checksum`` (default) only checksum-valid, correctly terminated
    frames are returned.
    """
    frames: list[Frame] = []
    i, n = 0, len(data)
    while i < n:
        if data[i] != START:
            i += 1
            continue
        # need: 68 L L 68 ... (at least the 4-byte header + CS + END)
        if i + 4 >= n:
            break
        length = data[i + 1]
        if data[i + 2] != length or data[i + 3] != START or length == 0:
            i += 1
            continue
        end_idx = i + 4 + length + 1          # index of the 0x16 end byte
        if end_idx >= n:
            i += 1
            continue
        user = data[i + 4:i + 4 + length]
        cs = data[i + 4 + length]
        ok = (checksum(user) == cs) and (data[end_idx] == END)
        if ok or not require_checksum:
            frames.append(Frame(control=user[0], user_data=user, checksum_ok=ok,
                                raw=data[i:end_idx + 1], start_offset=i))
            i = end_idx + 1
        else:
            i += 1
    return frames
