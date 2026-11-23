"""Raspberry Pi health and connection details for the dashboard (Linux /proc + ss, no extra deps)."""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import time

_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:  # noqa: BLE001 - dashboard data is best-effort
        val = None
    _cache[key] = (time.monotonic(), val)
    return val


def _run(cmd: list[str], timeout: float = 3) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def cpu_temp_c() -> float | None:
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except (OSError, ValueError):
        return None


def throttled() -> str | None:
    out = _cached("throttled", 30, lambda: _run(["vcgencmd", "get_throttled"]))
    if not out or "=" not in out:
        return None
    val = int(out.strip().split("=")[1], 16)
    if val == 0:
        return "ok"
    flags = {0: "under-voltage", 1: "freq-capped", 2: "throttled", 3: "temp-limit",
             16: "under-voltage (past)", 18: "throttled (past)"}
    return ", ".join(v for b, v in flags.items() if val & (1 << b)) or hex(val)


def memory() -> dict:
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) * 1024
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    return {"total": total, "used": total - avail, "pct": 100 * (total - avail) / total if total else 0}


def disk(path: str = "/") -> dict:
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free, "pct": 100 * u.used / u.total}


def uptime_s() -> float:
    with open("/proc/uptime") as fh:
        return float(fh.read().split()[0])


def addresses() -> dict[str, str]:
    def get():
        out = {}
        for itf in json.loads(_run(["ip", "-j", "-4", "addr"])):
            name = itf.get("ifname")
            if name == "lo":
                continue
            for a in itf.get("addr_info", []):
                out[name] = a.get("local")
        return out
    return _cached("addr", 30, get) or {}


def _classify(local_port: int, peer: str, peer_port: int, proc: str) -> str:
    if local_port == 22:
        return "ssh-in"
    if peer_port == 27017:
        return "mongodb"
    if peer_port == 22:
        return "ssh-out"
    if "tailscale" in proc or peer.startswith("100."):
        return "tailscale"
    if peer_port in (443, 80):
        return "https" if peer_port == 443 else "http"
    if local_port in (5900, 3389):
        return "remote-desktop"
    return "other"


def connections() -> dict:
    """Established TCP connections: total, by kind, and details (peer, process)."""
    def get():
        rows = []
        for line in _run(["ss", "-Htnp", "state", "established"]).splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            local, peer = parts[2], parts[3]
            proc = parts[4] if len(parts) > 4 else ""
            lhost, lport = local.rsplit(":", 1)
            phost, pport = peer.rsplit(":", 1)
            name = proc.split('"')[1] if '"' in proc else ""
            phost = phost.strip("[]").removeprefix("::ffff:")
            kind = _classify(int(lport), phost, int(pport), name)
            if phost in ("127.0.0.1", "::1"):
                kind = "local"
            rows.append({"kind": kind, "peer": f"{phost}:{pport}", "local_port": int(lport), "proc": name})
        by_kind: dict[str, int] = {}
        for r in rows:
            by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        return {"total": len(rows), "by_kind": by_kind, "rows": rows}
    return _cached("conn", 2, get) or {"total": 0, "by_kind": {}, "rows": []}


def logins() -> list[str]:
    def get():
        out = []
        for line in _run(["who"]).splitlines():
            p = line.split()
            if p:
                origin = p[-1].strip("()") if p[-1].startswith("(") else "local"
                out.append(f"{p[0]}@{p[1]} from {origin}")
        return out
    return _cached("who", 10, get) or []


def usb_devices() -> dict[str, str | None]:
    """Which testbed devices are plugged in (Airspy, ESP32 relay monitor, IR head)."""
    def get():
        by_id = glob.glob("/dev/serial/by-id/*")
        lsusb = _run(["lsusb"])
        esp = next((p for p in by_id if "CP210" in p or "Silicon_Labs" in p), None)
        ir = next((p for p in by_id if "FTDI" in p), None)
        return {"airspy": "Airspy HF+" if "03eb:800c" in lsusb.lower() else None,
                "esp32": os.path.realpath(esp) if esp else None,
                "ir_head": os.path.realpath(ir) if ir else None}
    return _cached("usb", 10, get) or {}


def service_state(name: str = "fre-platform") -> str:
    return _cached(f"svc:{name}", 5,
                   lambda: _run(["systemctl", "is-active", name]).strip() or "unknown") or "unknown"


def snapshot() -> dict:
    return {"host": socket.gethostname(), "temp_c": cpu_temp_c(), "load": os.getloadavg(),
            "cpus": os.cpu_count(), "mem": memory(), "disk": disk("/"), "uptime_s": uptime_s(),
            "throttled": throttled(), "addresses": addresses(), "connections": connections(),
            "logins": logins(), "usb": usb_devices()}
