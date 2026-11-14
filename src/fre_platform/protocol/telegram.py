"""Decode received radio ripple-control telegrams (receive-only, research).

Classifies parsed FT1.2 frames and decodes the broadcast time-synchronisation
telegram (EVU address 0x0000), which is plaintext and read by every receiver.

The time byte layout was reverse-engineered from the public BlinkenCity (38C3
2024) examples; minute/hour/day/weekday/month/year are confirmed against those
examples, the seconds field is provisional (marked ``seconds_provisional``).
Command telegrams (Versacom / Semagyr) are only classified at the header level
(control byte, EVU address, broad protocol guess) — the platform observes and
records them, it does not act on, craft or transmit any telegram.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .framing import Frame

# EFR weekday field: Sun=0 .. Sat=6 (value 6 == Saturday in the 38C3 examples)
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
BROADCAST_ADDR = 0x0000


@dataclass
class DecodedTime:
    year: int
    month: int
    day: int
    weekday: str
    hour: int
    minute: int
    second: int
    dst: bool
    seconds_provisional: bool = True
    iso: str | None = None


@dataclass
class Telegram:
    kind: str                     # "time" | "command" | "unknown"
    protocol: str                 # "din19244-time" | "versacom?" | "semagyr?" | "unknown"
    control: int
    evu_address: int
    checksum_ok: bool
    length: int
    raw_hex: str
    payload_hex: str
    time: DecodedTime | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evu_address"] = f"{self.evu_address:04x}" if self.evu_address >= 0 else None
        d["control"] = f"{self.control:02x}"
        if self.time is not None:
            d["time"] = {k: v for k, v in asdict(self.time).items()}
        return d


def decode_time_payload(payload: bytes) -> DecodedTime | None:
    """Decode the 7-byte time payload of a broadcast time telegram.

    Layout (offsets into payload, i.e. after control + 2-byte address):
      [0] flags/unknown   [1] second<<2   [2] minute   [3] dst<<7 | hour(7)
      [4] weekday<<5 | day(5)   [5] month   [6] year (2000+)
    """
    if len(payload) < 7:
        return None
    second = payload[1] >> 2
    minute = payload[2]
    hour = payload[3] & 0x7F
    dst = bool(payload[3] >> 7)
    day = payload[4] & 0x1F
    weekday_idx = payload[4] >> 5
    month = payload[5]
    year = 2000 + payload[6]
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 60):
        return None
    iso = None
    try:
        iso = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).isoformat()
    except ValueError:
        pass
    return DecodedTime(year=year, month=month, day=day,
                       weekday=WEEKDAYS[weekday_idx] if weekday_idx < 7 else str(weekday_idx),
                       hour=hour, minute=minute, second=second, dst=dst, iso=iso)


def classify(frame: Frame) -> Telegram:
    addr = frame.address
    payload = frame.payload
    tg = Telegram(kind="unknown", protocol="unknown", control=frame.control, evu_address=addr,
                  checksum_ok=frame.checksum_ok, length=frame.length, raw_hex=frame.hex(),
                  payload_hex=payload.hex())
    if addr == BROADCAST_ADDR and len(payload) >= 7:
        t = decode_time_payload(payload)
        if t is not None:
            tg.kind, tg.protocol, tg.time = "time", "din19244-time", t
            return tg
        tg.notes.append("broadcast address but time payload did not validate")
    if addr not in (BROADCAST_ADDR, -1):
        tg.kind = "command"
        # Heuristic protocol hint from control-byte low nibble (Versacom vs Semagyr-TOP)
        tg.protocol = "versacom?" if (frame.control & 0x0F) in (0x7,) else "semagyr?"
        tg.notes.append("command telegram observed (header only; not decoded or acted upon)")
    return tg


def decode_bytes(data: bytes, require_checksum: bool = True) -> list[Telegram]:
    from .framing import parse_frames
    return [classify(f) for f in parse_frames(data, require_checksum=require_checksum)]


def assess_authenticity(telegram: dict, received_ts: datetime, signal: dict | None = None,
                        ntp_synced: bool | None = None, tolerance_s: float = 5.0) -> dict:
    """Deterministic authenticity signals for a decoded telegram (no ML).

    These features let a researcher - and later a model - separate authentic
    broadcasts from replayed / spoofed / modified ones:

    * ``checksum_ok``   - frame integrity (a modified frame usually fails this)
    * ``clock_offset_s``- for time telegrams: decoded time minus the receiver's
      NTP clock; a large magnitude indicates replay or a spoofed timestamp
      (BlinkenCity Attack 1 - the time telegram has no integrity/replay protection)
    * ``time_plausible``- offset within tolerance and the receiver clock trusted
    * ``signal``        - received-signal fingerprint (SNR, power, carrier offset);
      a local/rogue emitter overpowering the distant 100 kW tower looks different
    * ``assessment``    - compact verdict: authentic_candidate / implausible /
      integrity_fail / unverified
    """
    out: dict = {"checksum_ok": bool(telegram.get("checksum_ok")), "ntp_synced": ntp_synced}
    if signal:
        out["signal"] = {k: signal.get(k) for k in ("snr_db", "power_dbfs", "carrier_offset_hz", "noise_floor_db")}
    time = telegram.get("time") or {}
    iso = time.get("iso")
    if iso:
        try:
            decoded = datetime.fromisoformat(iso)
            offset = (decoded - received_ts).total_seconds()
            out["clock_offset_s"] = round(offset, 3)
            out["time_plausible"] = bool(abs(offset) <= tolerance_s and ntp_synced is not False)
        except ValueError:
            pass
    if not out["checksum_ok"]:
        out["assessment"] = "integrity_fail"
    elif out.get("time_plausible") is False:
        out["assessment"] = "implausible"
    elif out.get("time_plausible") is True:
        out["assessment"] = "authentic_candidate"
    else:
        out["assessment"] = "unverified"   # e.g. command telegram: no decodable integrity reference
    return out
