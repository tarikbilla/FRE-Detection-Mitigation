"""Telegram framing + decoding, verified against the public BlinkenCity (38C3) examples."""
from datetime import datetime, timezone

import numpy as np
import pytest

from fre_platform.protocol import checksum, decode_bytes, parse_frames
from fre_platform.protocol.telegram import decode_time_payload

# Real telegrams quoted in the 38C3 "BlinkenCity" talk (slides 11-12).
EXAMPLES = [
    "68 0a 0a 68 67 00 00 00 08 00 92 2a 06 18 49 16",
    "68 06 06 68 17 d3 d1 e4 00 80 1f 16",
    "68 08 08 68 27 b0 b0 3c 00 20 80 c0 23 16",
    "68 09 09 68 37 b0 b0 3e 00 10 80 c0 55 7a 16",
]
TIME_EXAMPLES = [
    ("68 0a 0a 68 57 00 00 00 80 2f 0b de 0b 18 12 16", (2024, 11, 30, 11, 47, "Sat")),
    ("68 0a 0a 68 b7 00 00 00 80 30 0b de 0b 18 73 16", (2024, 11, 30, 11, 48, "Sat")),
]


def _b(s):
    return bytes.fromhex(s.replace(" ", ""))


@pytest.mark.parametrize("hexstr", EXAMPLES + [t[0] for t in TIME_EXAMPLES])
def test_real_frames_parse_and_checksum(hexstr):
    frames = parse_frames(_b(hexstr))
    assert len(frames) == 1 and frames[0].checksum_ok
    assert frames[0].raw == _b(hexstr)


def test_checksum_matches_din():
    # 67 00 00 00 08 00 92 2a 06 18 -> 0x49
    assert checksum(_b("67 00 00 00 08 00 92 2a 06 18")) == 0x49


@pytest.mark.parametrize("hexstr,expect", TIME_EXAMPLES)
def test_time_telegram_decode(hexstr, expect):
    tgs = decode_bytes(_b(hexstr))
    assert len(tgs) == 1
    t = tgs[0]
    assert t.kind == "time" and t.protocol == "din19244-time" and t.evu_address == 0x0000
    y, mo, d, h, mi, wd = expect
    assert (t.time.year, t.time.month, t.time.day) == (y, mo, d)
    assert (t.time.hour, t.time.minute, t.time.weekday) == (h, mi, wd)


def test_command_telegram_classified_not_decoded():
    t = decode_bytes(_b("68 08 08 68 27 b0 b0 3c 00 20 80 c0 23 16"))[0]
    assert t.kind == "command" and t.evu_address == 0xb0b0 and t.checksum_ok
    assert t.time is None  # command telegrams are never decoded/acted upon


def test_resync_after_garbage():
    data = b"\x00\xff\x12" + _b(EXAMPLES[0]) + b"\x99" + _b(EXAMPLES[2])
    frames = parse_frames(data)
    assert len(frames) == 2


def test_bad_checksum_rejected_but_recoverable():
    bad = bytearray(_b(EXAMPLES[0]))
    bad[-2] ^= 0xFF
    assert parse_frames(bytes(bad)) == []
    assert decode_bytes(bytes(bad), require_checksum=False)[0].checksum_ok is False


def test_decode_time_payload_rejects_invalid():
    assert decode_time_payload(bytes([0, 0, 99, 0, 0, 99, 0])) is None  # minute 99, month 99


# -- full receive chain: synth baseband -> FSK demod -> framer -> decoder -------
def test_fsk_demod_recovers_time_telegram():
    from fre_platform.dsp.demod import FskDemodulator
    from fre_platform.protocol.synth import time_telegram_baseband

    fs = 4000.0
    ts = datetime(2024, 11, 30, 11, 48, 22, tzinfo=timezone.utc)
    bb, frame = time_telegram_baseband(ts, fs)
    demod = FskDemodulator(fs)
    out = demod.process(bb)
    assert frame in out, (out.hex(), frame.hex())
    tgs = decode_bytes(out)
    assert any(t.kind == "time" and t.time.hour == 11 and t.time.minute == 48 for t in tgs)


def test_fsk_demod_survives_noise_and_block_split():
    from fre_platform.dsp.demod import FskDemodulator
    from fre_platform.protocol.synth import time_telegram_baseband

    fs = 4000.0
    rng = np.random.default_rng(0)
    bb, frame = time_telegram_baseband(datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc), fs, amplitude=1.0)
    bb = bb + (rng.standard_normal(len(bb)) + 1j * rng.standard_normal(len(bb))).astype(np.complex64) * 0.05
    demod = FskDemodulator(fs)
    out = bytearray()
    for i in range(0, len(bb), 777):            # feed in uneven chunks across "blocks"
        out += demod.process(bb[i:i + 777])
    assert frame in bytes(out)
