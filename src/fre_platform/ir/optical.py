"""Read-only IEC 62056-21 (IEC 1107) probe of the FRE's optical port.

Integrated from the "FRE Reader" tool (fre_reader.py). Hardware: Emlog IR
read/write head (FTDI FT232R) on the FRE's optical port (Langmatz EK 393,
LK2-E5-0455; manufacturer flag "LAN").

Each probe (mode C):
  1. 300 Bd 7E1, send ``/?!<CR><LF>`` (sign-on to any address)
  2. expect ``/XXXZ<ident><CR><LF>`` (XXX = manufacturer, Z = baud code)
  3. if identified: ACK ``<ACK>0Z0<CR><LF>`` (option 0 = data readout only),
     switch to the announced baud, read ``STX ... ! CR LF ETX BCC``

Only the sign-on and the readout-mode ACK are ever sent - never programming
mode, passwords or writes. The head hears its own transmission (echo); that
echo is stripped and recorded as proof that the head is working.

Observation 23 Nov 2026 (bench, Mac): the unit did not answer any sign-on
(300-28800 Bd, 7E1/8N1, address variants, wake-up preamble, 30 min run). Every
probe is therefore stored (``responded: false`` included) - the port's
behaviour over time is research evidence in itself.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import SCHEMA_VERSION
from ..config import Settings
from ..relay.monitor import resolve_port
from ..timeid import make_id

log = logging.getLogger(__name__)

BAUD_CODES = {"0": 300, "1": 600, "2": 1200, "3": 2400, "4": 4800, "5": 9600, "6": 19200}
SIGN_ON = b"/?!\r\n"
PORT_GLOBS = ("/dev/serial/by-id/*FTDI*",)


def strip_echo(sent: bytes, received: bytes) -> tuple[bool, bytes]:
    """(echo_seen, payload): remove the head's echo of ``sent`` from the start."""
    if received.startswith(sent):
        return True, received[len(sent):]
    return False, received


def parse_ident(resp: bytes) -> dict | None:
    """``/LAN5xxxx`` -> {manufacturer, baud_code, baud, ident}; None if not an identification."""
    line = resp.strip(b"\r\n\x00 ")
    if not line.startswith(b"/") or len(line) < 5:
        return None
    text = line.decode("ascii", "replace")
    code = text[4]
    return {"manufacturer": text[1:4], "baud_code": code, "baud": BAUD_CODES.get(code, 300),
            "ident": text[5:], "line": text}


def parse_data_block(data: bytes) -> dict:
    """``STX body ! CR LF ETX BCC`` -> {lines, bcc_ok}; tolerant of missing parts."""
    out: dict = {"lines": [], "bcc_ok": None, "complete": False}
    if b"\x02" not in data:
        return out
    start = data.index(b"\x02") + 1
    end = data.find(b"\x03", start)
    body = data[start:end if end != -1 else len(data)]
    if end != -1 and end + 1 < len(data):
        bcc = 0
        for x in data[start:end + 1]:
            bcc ^= x
        out["bcc_ok"] = bcc == data[end + 1]
        out["complete"] = True
    out["lines"] = [ln for ln in body.decode("ascii", "replace").splitlines() if ln.strip() and ln.strip() != "!"]
    return out


def _read_until_quiet(ser, overall: float, quiet: float) -> bytes:
    buf = bytearray()
    t0 = last = time.monotonic()
    while time.monotonic() - t0 < overall:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            last = time.monotonic()
        elif buf and time.monotonic() - last > quiet:
            break
        time.sleep(0.02)
    return bytes(buf)


def probe_once(port: str) -> dict:
    """One sign-on (+ readout if the FRE identifies itself). Returns a result dict."""
    import serial
    t0 = time.monotonic()
    res: dict = {"port": port, "echo": False, "responded": False, "ident": None, "readout": None,
                 "rx_hex": "", "error": None}
    ser = serial.Serial(port, 300, bytesize=serial.SEVENBITS, parity=serial.PARITY_EVEN,
                        stopbits=1, timeout=0, exclusive=True)  # lock: service and manual probe never collide
    try:
        ser.reset_input_buffer()
        ser.write(SIGN_ON)
        ser.flush()
        # IEC 62056-21: the device answers within 200 ms - 1.5 s
        echo, payload = strip_echo(SIGN_ON, _read_until_quiet(ser, overall=5.0, quiet=2.5))
        res["echo"] = echo
        res["rx_hex"] = payload.hex(" ")[:2000]
        res["responded"] = bool(payload.strip(b"\x00"))
        ident = parse_ident(payload)
        if ident:
            res["ident"] = ident
            ack = b"\x06" + b"0" + ident["baud_code"].encode() + b"0\r\n"
            ser.write(ack)
            ser.flush()
            time.sleep(0.3)
            ser.baudrate = ident["baud"]
            ser.reset_input_buffer()
            _, data = strip_echo(ack, _read_until_quiet(ser, overall=60.0, quiet=2.5))
            block = parse_data_block(data)
            res["readout"] = {**block, "bytes": len(data), "raw": data.decode("ascii", "replace")[:20000]}
    finally:
        ser.close()
    res["duration_s"] = round(time.monotonic() - t0, 2)
    return res


class IrProbe(threading.Thread):
    """Periodic read-only probe. Never raises out of ``run``."""

    def __init__(self, cfg: Settings, buffer=None, probe_fn=probe_once):
        super().__init__(name="ir-probe", daemon=True)
        self.cfg = cfg
        self.buffer = buffer
        self.probe_fn = probe_fn
        self.live_path = Path(cfg.data_dir) / "ir_live.json"
        self._stop_evt = threading.Event()
        self._wake = threading.Event()   # set by probe_now(): probe immediately
        self.last_result: dict | None = None
        self.state = {"port": None, "head_present": False, "probes": 0, "responses": 0, "echo_ok": None,
                      "last_probe_ts": None, "last_response_ts": None, "last_ident": None,
                      "last_lines": [], "last_error": None, "next_probe_ts": None,
                      "interval_s": cfg.ir_probe_interval_s, "recent": []}

    def doc(self, res: dict, ts: datetime) -> dict:
        return {"_id": make_id(ts, self.cfg.station_id, "ir"),
                "schema_version": SCHEMA_VERSION, "dataset_version": self.cfg.dataset_version,
                "experiment_id": self.cfg.ir_experiment_id, "station_id": self.cfg.station_id,
                "ts": ts, "source": "ir-optical", "protocol": "IEC 62056-21 mode C (read-only)",
                "port": res.get("port"), "echo": res.get("echo"), "responded": bool(res.get("responded")),
                "ident": res.get("ident"), "readout": res.get("readout"), "rx_hex": res.get("rx_hex"),
                "duration_s": res.get("duration_s"), "error": res.get("error")}

    def write_live(self) -> None:
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            tmp = self.live_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, default=str))
            tmp.replace(self.live_path)
        except OSError:
            log.debug("could not write ir_live.json", exc_info=True)

    def run(self) -> None:
        Path(self.cfg.data_dir).mkdir(parents=True, exist_ok=True)
        announced_missing = False
        while not self._stop_evt.is_set():
            port = resolve_port(self.cfg.ir_port, PORT_GLOBS)
            self.state["port"] = port
            self.state["head_present"] = bool(port)
            if not port:
                if not announced_missing:
                    log.info("IR head not connected; checking every 30 s", extra={"glob": PORT_GLOBS[0]})
                    announced_missing = True
                self.state["next_probe_ts"] = None
                self.last_result = {"responded": False, "error": "IR head not connected", "port": None}
                self.write_live()
                self._sleep(30)
                continue
            announced_missing = False
            ts = datetime.now(timezone.utc)
            try:
                res = self.probe_fn(port)
            except Exception as exc:  # noqa: BLE001 - port busy, head unplugged mid-probe, ...
                res = {"port": port, "echo": False, "responded": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
                log.warning("IR probe failed", extra={"port": port, "error": res["error"]})
            self.last_result = res
            st = self.state
            st["probes"] += 1
            st["last_probe_ts"] = ts.isoformat(timespec="seconds")
            st["echo_ok"] = res.get("echo")
            st["last_error"] = res.get("error")
            if res.get("responded"):
                st["responses"] += 1
                st["last_response_ts"] = st["last_probe_ts"]
                if res.get("ident"):
                    st["last_ident"] = res["ident"]["line"]
                if res.get("readout"):
                    st["last_lines"] = res["readout"]["lines"][:40]
                log.info("FRE answered on the IR port", extra={"ident": st["last_ident"]})
            st["recent"] = ([{"ts": st["last_probe_ts"], "echo": res.get("echo"), "responded": res.get("responded"),
                              "ident": (res.get("ident") or {}).get("line"), "error": res.get("error")}]
                            + st["recent"])[:10]
            if self.buffer is not None:
                try:
                    self.buffer.put("ir_readouts", self.doc(res, ts), "insert")
                except Exception:  # noqa: BLE001
                    log.exception("could not buffer IR probe result")
            st["next_probe_ts"] = datetime.fromtimestamp(ts.timestamp() + self.cfg.ir_probe_interval_s,
                                                          timezone.utc).isoformat(timespec="seconds")
            self.write_live()
            self._sleep(self.cfg.ir_probe_interval_s)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def probe_now(self) -> int:
        """Request an immediate probe; returns the probe counter to wait for (> this value)."""
        n = self.state["probes"]
        self._wake.set()
        return n

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
