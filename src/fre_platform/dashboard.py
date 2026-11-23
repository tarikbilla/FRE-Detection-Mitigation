"""Four-panel terminal dashboard (rich renderers) for the integrated FRE platform.

    ┌──────────────────────────────┬──────────────────────────────┐
    │ 1 · SDR & RASPBERRY PI       │ 2 · FRE RECEIVER             │
    │  reception, Pi health,       │  K1..K4 relays, events,      │
    │  network                     │  optical (IR) port           │
    ├──────────────────────────────┼──────────────────────────────┤
    │ 3 · MONGODB fre_dataset_v2   │ 4 · BACKUPS & CONFIG         │
    └──────────────────────────────┴──────────────────────────────┘

The panels are used by the Textual app (``tui.py``) and by the classic
read-only dashboard (``fre --classic``). They read only the JSON state files the
service publishes in ``data_dir`` plus local system information.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from rich import box
from rich.console import Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import __version__, sysinfo
from .config import Settings

# ------------------------------------------------------------------ palette
TEXT = "#41ff7f"          # normal text
VAL = "bold #d2ffdd"      # values
LBL = "#46b46c"           # labels (readable on black)
DIM = "#2a7a45"           # secondary information
OK = "bold #39ff6a"
WARN = "bold #ffd166"
BAD = "bold #ff5555"
BORDER = "#1fa64a"
# kept for backwards compatibility with older imports
G, G2, HI = TEXT, "#7dff9b", OK

CHIP = {"ok": "bold #001a08 on #39ff6a", "warn": "bold #1a1400 on #ffd166",
        "bad": "bold #ffffff on #c0392b", "info": "bold #d2ffdd on #1b3d26"}
DOT = {"ok": "●", "warn": "▲", "bad": "✖", "info": "○"}


# ----------------------------------------------------------------- helpers
def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return None


def _dur(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h {s % 3600 // 60:02d} m"
    return f"{s // 86400} d {s % 86400 // 3600} h"


def _ago(iso: str | None) -> str:
    a = _age_s(iso)
    return "never" if a is None else f"{_dur(a)} ago"


def _bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


def _num(v, fmt: str) -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else "-"


def _bar(frac: float, width: int = 18, warn: float = 0.75, bad: float = 0.9) -> Text:
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    style = BAD if frac >= bad else WARN if frac >= warn else OK
    return Text("━" * n, style=style) + Text("─" * (width - n), style=DIM)


SPARK = "▁▂▃▄▅▆▇█"


def _spark(values, lo: float = -10.0, hi: float = 45.0, width: int = 24, style: str = "#7dff9b") -> Text:
    vals = [v for v in list(values)[-width:] if isinstance(v, (int, float))]
    if not vals:
        return Text("·" * width, style=DIM)
    span = (hi - lo) or 1.0
    chars = "".join(SPARK[max(0, min(7, int((v - lo) / span * 7.999)))] for v in vals)
    return Text(chars.rjust(width, "·"), style=style)


def update_history(prev: dict, live: dict | None, maxlen: int = 120) -> None:
    """Remember per-station SNR of every new SDR block (for the trend sparklines)."""
    if not live or prev.get("_snr_seq") == live.get("seq"):
        return
    prev["_snr_seq"] = live.get("seq")
    hist = prev.setdefault("_snr", {})
    for c in live.get("channels", []):
        h = hist.setdefault(c["name"], [])
        h.append(c.get("snr_db"))
        del h[:-maxlen]


def _stamp(name: str) -> str:
    """2026-11-24T033000Z -> 2026-11-24 03:30:00Z (legacy day-only names pass through)."""
    if len(name) == 18 and name[10] == "T":
        return f"{name[:10]} {name[11:13]}:{name[13:15]}:{name[15:17]}Z"
    return name


def _line(*parts) -> Text:
    """Label/value pairs on one line: _line(("block", "#12"), ("data", Text(...)), "free text")."""
    t = Text()
    for i, p in enumerate(parts):
        if i:
            t.append("   ")
        if isinstance(p, tuple):
            k, v = p
            if k:
                t.append(f"{k} ", style=LBL)
            t.append_text(v if isinstance(v, Text) else Text(str(v), style=VAL))
        else:
            t.append_text(p if isinstance(p, Text) else Text(str(p), style=TEXT))
    return t


def _section(title: str) -> Rule:
    return Rule(Text(f" {title} ", style="bold #7dff9b"), characters="─", style=DIM, align="left")


def _panel(body, title: str, subtitle: str | None = None) -> Panel:
    return Panel(body, title=Text(f" {title} ", style="bold #001a08 on #39ff6a"), title_align="left",
                 subtitle=Text(subtitle, style=DIM) if subtitle else None, subtitle_align="right",
                 border_style=BORDER, box=box.ROUNDED, padding=(0, 1), style=f"{TEXT} on #000000")


# ------------------------------------------------------------------- health
def health(cfg: Settings, sysi: dict | None = None) -> list[dict]:
    """One status per subsystem: {name, level (ok|warn|bad|info), text}."""
    d = Path(cfg.data_dir)
    live, relay, ir = _load(d / "live.json"), _load(d / "relay_live.json"), _load(d / "ir_live.json")
    plat = _load(d / "platform.json") or {}
    subs = plat.get("subsystems") or {}
    out = []

    age = _age_s((live or {}).get("ts"))
    if age is None or age > 30:
        out.append({"name": "SDR", "level": "bad", "text": "no SDR data" + (f" for {_dur(age)}" if age else "")})
    elif not live.get("valid"):
        out.append({"name": "SDR", "level": "warn", "text": "capture invalid: " + "; ".join(live.get("reasons", []))})
    else:
        recv = sum(1 for c in live.get("channels", []) if c.get("signal_state") == "present")
        out.append({"name": "SDR", "level": "ok", "text": f"{recv} stations received"})

    if not relay:
        out.append({"name": "ESP32", "level": "bad", "text": "relay monitor not running"})
    else:
        la = _age_s(relay.get("last_line_ts"))
        if not relay.get("connected"):
            out.append({"name": "ESP32", "level": "bad", "text": "ESP32 disconnected"})
        elif relay.get("in_gap") or la is None or la > 180:
            out.append({"name": "ESP32", "level": "warn", "text": "ESP32 silent (no heartbeat)"})
        else:
            out.append({"name": "ESP32", "level": "ok", "text": "relay monitor live"})

    if not ir or not cfg.ir_enabled:
        out.append({"name": "IR", "level": "info", "text": "IR probe off"})
    elif not ir.get("head_present"):
        out.append({"name": "IR", "level": "warn", "text": "IR head not connected"})
    elif ir.get("responses"):
        out.append({"name": "IR", "level": "ok", "text": "FRE answers on the optical port"})
    else:
        out.append({"name": "IR", "level": "info", "text": "optical port silent (expected for this unit)"})

    up = subs.get("uploader") or {}
    if not up.get("enabled", True) or not up:
        out.append({"name": "MONGO", "level": "warn", "text": "uploads disabled"})
    elif not up.get("online"):
        out.append({"name": "MONGO", "level": "bad", "text": f"Atlas offline - buffering ({(plat.get('buffer') or {}).get('rows', 0)} rows)"})
    elif up.get("quota_restricted"):
        out.append({"name": "MONGO", "level": "warn", "text": "quota guard active: bulk data held back"})
    else:
        out.append({"name": "MONGO", "level": "ok", "text": f"{cfg.mongodb_db} online"})

    bk = subs.get("backup") or {}
    from .backup import list_backups
    backups = list_backups(cfg)
    last_age = _age_s(backups[0].get("finished_at")) if backups else None
    if bk.get("last_error"):
        out.append({"name": "BACKUP", "level": "bad", "text": "last backup failed"})
    elif bk.get("running"):
        out.append({"name": "BACKUP", "level": "info", "text": "backup running"})
    elif last_age is None or last_age > 26 * 3600:
        out.append({"name": "BACKUP", "level": "warn", "text": "no backup in the last 26 h"})
    else:
        out.append({"name": "BACKUP", "level": "ok", "text": f"last backup {_dur(last_age)} ago"})

    if sysi:
        temp, dpct = sysi.get("temp_c") or 0, sysi["disk"]["pct"]
        if temp > 80 or dpct > 95:
            out.append({"name": "PI", "level": "bad", "text": f"{temp:.0f} °C, disk {dpct:.0f} %"})
        elif temp > 75 or dpct > 90 or sysi.get("throttled") not in (None, "ok"):
            out.append({"name": "PI", "level": "warn",
                        "text": f"{temp:.0f} °C, disk {dpct:.0f} %, {sysi.get('throttled') or ''}".strip(", ")})
        else:
            out.append({"name": "PI", "level": "ok", "text": f"{temp:.0f} °C"})
    age = _age_s(plat.get("updated_at"))
    if age is None or age > 20:
        out.insert(0, {"name": "CORE", "level": "bad", "text": "platform not running (no heartbeat)"})
    return out


def chips(states: list[dict]) -> Text:
    t = Text()
    for s in states:
        t.append(f" {DOT[s['level']]} {s['name']} ", style=CHIP[s["level"]])
        t.append(" ")
    return t


def alerts(states: list[dict]) -> Text | None:
    bad = [s for s in states if s["level"] in ("bad", "warn")]
    if not bad:
        return None
    t = Text(" ⚠ ", style=BAD)
    for i, s in enumerate(bad):
        if i:
            t.append("  ·  ", style=DIM)
        t.append(f"{s['name']}: ", style=BAD if s["level"] == "bad" else WARN)
        t.append(s["text"], style=TEXT)
    return t


# ------------------------------------------------------------------ panels
def _snr_style(snr) -> str:
    if not isinstance(snr, (int, float)):
        return DIM
    return OK if snr >= 20 else TEXT if snr >= 10 else DIM


def panel_sdr(live: dict | None, status: dict | None, sysi: dict, history: dict | None = None) -> Panel:
    rows: list = [_section("SDR · AIRSPY HF+")]
    if live:
        age = _age_s(live.get("ts"))
        fresh = age is not None and age < 15
        rows.append(_line(("block", f"#{live['seq']:,}"),
                          ("data", Text(_ago(live.get("ts")), style=OK if fresh else BAD)),
                          ("capture", Text("OK", style=OK) if live.get("valid") else Text("INVALID", style=BAD)),
                          ("floor", f"{_num(live.get('band_floor_db'), '.1f')} dBFS/Hz")))
        t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {LBL}", border_style=DIM, pad_edge=False,
                  padding=(0, 1), expand=True, show_edge=False)
        t.add_column("STATION", no_wrap=True)
        t.add_column("kHz", justify="right", no_wrap=True)
        t.add_column("SNR dB", justify="right", no_wrap=True)
        t.add_column("TREND · 2 min", no_wrap=True, ratio=1)
        t.add_column("STATE", no_wrap=True)
        t.add_column("FSK", justify="right", no_wrap=True)
        t.add_column("LAST TELEGRAM", no_wrap=True)
        for c in live.get("channels", []):
            recv = c.get("signal_state") == "present"
            snr = c.get("snr_db")
            last = ""
            for tg in c.get("telegrams", []):
                last = f"{tg['kind']} · {tg.get('assessment') or '?'}"
            lt = Text(last, style=BAD if ("implausible" in last or "integrity" in last) else "#7dff9b")
            t.add_row(Text(c["name"], style=VAL), f"{c['freq_hz'] / 1e3:.1f}", Text(_num(snr, ".1f"), style=_snr_style(snr)),
                      _spark((history or {}).get(c["name"], []), width=28,
                             style="#7dff9b" if recv else DIM),
                      Text("● RECV", style=OK) if recv else Text("○ none", style=DIM),
                      _num(c.get("fsk_activity"), ".2f"), lt)
        rows.append(t)
        tot = live.get("telegram_totals") or {}
        total, tm, sus = tot.get("total", 0), tot.get("time", 0), tot.get("suspect", 0)
        src = (status or {}).get("source_stats") or {}
        rows.append(_line(("telegrams", f"{total:,}"), ("time", f"{tm:,}"), ("command", f"{total - tm:,}"),
                          ("suspect", Text(str(sus), style=BAD if sus else VAL)),
                          ("restarts", src.get("restarts", 0)), ("dropped", src.get("dropped_blocks", 0))))
    else:
        rows.append(Text("waiting for the SDR collector ...", style=WARN))

    rows.append(_section("RASPBERRY PI"))
    mem, dsk = sysi["mem"], sysi["disk"]
    temp = sysi.get("temp_c")
    rows.append(_line(("host", sysi["host"]), ("up", _dur(sysi["uptime_s"])),
                      ("temp", Text(f"{_num(temp, '.0f')} °C", style=BAD if (temp or 0) > 75 else VAL)),
                      ("load", " ".join(f"{x:.2f}" for x in sysi["load"]))))
    rows.append(_line(("mem", _bar(mem["pct"] / 100, 14)), ("", f"{mem['pct']:.0f} %"),
                      ("disk", _bar(dsk["pct"] / 100, 14)), ("", f"{dsk['pct']:.0f} %  {_bytes(dsk['free'])} free")))
    usb = sysi.get("usb") or {}
    dev = Text()
    for name, key in (("Airspy", "airspy"), ("ESP32", "esp32"), ("IR head", "ir_head")):
        dev.append("✔ " if usb.get(key) else "✘ ", style=OK if usb.get(key) else BAD)
        dev.append(f"{name}   ", style=VAL if usb.get(key) else BAD)
    rows.append(_line(("usb", dev)))
    if sysi.get("addresses"):
        rows.append(_line(*[(k, v) for k, v in sysi["addresses"].items()]))
    if sysi.get("throttled") not in (None, "ok"):
        rows.append(Text(f"⚠ power/thermal: {sysi['throttled']}", style=BAD))

    conn = sysi.get("connections") or {}
    rows.append(_section(f"NETWORK · {conn.get('total', 0)} CONNECTIONS"))
    rows.append(_line(*[(k, v) for k, v in sorted((conn.get("by_kind") or {}).items(), key=lambda kv: -kv[1])]))
    seen = set()
    order = {"ssh-in": 0, "remote-desktop": 1, "tailscale": 2, "ssh-out": 3}
    for r in sorted(conn.get("rows", []), key=lambda r: order.get(r["kind"], 9)):
        key = (r["kind"], r["peer"].rsplit(":", 1)[0])
        if r["kind"] not in order or key in seen:
            continue
        seen.add(key)
        rows.append(Text.assemble(("  ⇄ ", DIM), (f"{r['kind']:<10}", LBL), (f"{r['peer']:<24}", VAL),
                                  (r["proc"], DIM)))
        if len(seen) >= 4:
            break
    return _panel(Group(*rows), "1 · SDR & RASPBERRY PI", "receive-only")


LEVEL = {"K1": "100 %", "K2": "60 %", "K3": "30 %", "K4": "0 %"}


def panel_fre(relay: dict | None, ir: dict | None) -> Panel:
    rows: list = [_section("RELAYS K1..K4 · ESP32 MONITOR")]
    if relay:
        line_age = _age_s(relay.get("last_line_ts"))
        alive = relay.get("connected") and line_age is not None and line_age < 120
        rows.append(_line(("esp32", Text("CONNECTED", style=OK) if relay.get("connected")
                           else Text("DISCONNECTED", style=BAD)),
                          ("fw", relay.get("fw") or "?"),
                          ("last line", Text(_ago(relay.get("last_line_ts")), style=VAL if alive else WARN)),
                          ("events today", f"{relay.get('events_today', 0):,}")))
        if relay.get("error") and not relay.get("connected"):
            rows.append(Text(f"  {relay['error']}", style=BAD))
        if relay.get("in_gap"):
            rows.append(Text("  ⚠ ESP32 silent - heartbeat missing", style=BAD))
        t = Table(box=box.ROUNDED, border_style=DIM, show_header=True, header_style=f"bold {LBL}",
                  padding=(0, 1), expand=True)
        pos = relay.get("positions") or {}
        cells = []
        for r in ("K1", "K2", "K3", "K4"):
            t.add_column(f"{r} · {LEVEL[r]}", justify="center", ratio=1)
            p = pos.get(r)
            cells.append(Text("■ ACTIVE  a", style="bold #001a08 on #39ff6a") if p == "a" else
                         Text("□ rest  b", style=TEXT) if p == "b" else Text("? unknown", style=DIM))
        t.add_row(*cells)
        rows.append(t)
        lvl = relay.get("level_pct")
        if lvl is not None:
            rows.append(_line(("requested feed-in", Text(f"{lvl} %", style=OK))))
        else:
            rows.append(_line(("requested feed-in", Text("no single level", style=WARN)),
                              Text("all four at a = this unit's idle pattern / switchover", style=DIM)))
        for ev in (relay.get("recent") or [])[:6]:
            ts = (ev.get("ts") or "")[11:19]
            if ev.get("type") == "event":
                arrow = Text(f"{ev['relay']} → {ev['position']}", style=OK if ev["position"] == "a" else VAL)
                rows.append(Text.assemble(("  ", ""), (ts, LBL), ("  ", ""), arrow,
                                          (f"   {ev.get('level_pct')} %", TEXT), (f"   {_ago(ev.get('ts'))}", DIM)))
            else:
                rows.append(Text.assemble(("  ", ""), (ts, DIM), ("  ", ""),
                                          (f"{ev['type']} {ev.get('detail') or ''}"[:60], DIM)))
    else:
        rows.append(Text("relay monitor not running", style=WARN))

    rows.append(_section("OPTICAL PORT · IEC 62056-21 (read-only)"))
    if ir:
        rows.append(_line(("head", Text("✔ present", style=OK) if ir.get("head_present")
                           else Text("✘ not connected", style=WARN)),
                          ("echo", Text("✔", style=OK) if ir.get("echo_ok") else Text("-", style=DIM)),
                          ("probes", f"{ir.get('probes', 0):,}"),
                          ("answers", Text(str(ir.get("responses", 0)), style=OK if ir.get("responses") else VAL)),
                          ("next", (ir.get("next_probe_ts") or "-")[11:19] or "-")))
        if ir.get("last_ident"):
            rows.append(Text(f"  ident {ir['last_ident']}", style=OK))
            for line in (ir.get("last_lines") or [])[:6]:
                rows.append(Text(f"    {line}", style=VAL))
        elif ir.get("probes"):
            rows.append(Text("  FRE does not answer on its optical port - expected for this unit "
                             "(vendor parametrisation interface)", style=DIM))
        if ir.get("last_error"):
            rows.append(Text(f"  {ir['last_error']}", style=WARN))
    else:
        rows.append(Text("IR probe not running", style=DIM))
    return _panel(Group(*rows), "2 · FRE RECEIVER", "LK2-E5-0455 · 129.1 kHz")


def panel_mongo(stats: dict | None, plat: dict | None, prev: dict) -> Panel:
    rows: list = []
    up = ((plat or {}).get("subsystems") or {}).get("uploader") or {}
    buf = (plat or {}).get("buffer") or {}
    rows.append(_line(("atlas", Text("● ONLINE", style=OK) if up.get("online") else Text("✖ OFFLINE", style=BAD)),
                      ("latency", f"{_num((stats or {}).get('latency_ms'), '.0f')} ms"),
                      ("uploaded", f"{up.get('uploaded', 0):,}"), ("last ok", _ago(up.get("last_success")))))
    rows.append(_line(("buffer", Text(f"{buf.get('rows', 0):,}", style=WARN if buf.get("rows", 0) > 500 else VAL)),
                      ("dead-letter", Text(str(buf.get("dead_letter", 0)), style=BAD if buf.get("dead_letter") else VAL)),
                      ("rejected", up.get("rejected", 0)), ("failures", up.get("failures", 0))))
    if up.get("last_error") and not up.get("online"):
        rows.append(Text(f"  {up['last_error'][:90]}", style=BAD))
    if stats:
        used, quota = stats.get("cluster_used_mb", 0), stats.get("quota_mb", 512)
        rows.append(_line(("quota", _bar(used / quota if quota else 0, 22, 0.7, stats.get("soft_limit_pct", 85) / 100)),
                          ("", f"{used:.0f} / {quota:.0f} MB"),
                          Text(f"(incl. {stats.get('reserved_mb'):.0f} MB other data)", style=DIM)
                          if stats.get("reserved_mb") else ""))
        if up.get("quota_restricted"):
            rows.append(Text("  ⚠ quota guard active: spectra/observations held back locally", style=WARN))
        pending = buf.get("by_collection") or {}
        dbs = stats.get("databases") or {}
        active_name = stats.get("active_db") or next((n for n, d in dbs.items() if d.get("active")), "")
        active = dbs.get(active_name)
        if active:
            # ingest rate history: one sample per new statistics snapshot
            hist = prev.setdefault("_ingest", [])
            if not hist or hist[-1][0] != stats.get("updated_at"):
                hist.append((stats.get("updated_at"), time.time(), active["documents"]))
                del hist[:-40]
            rates = [(b[2] - a[2]) / max(1e-9, (b[1] - a[1]) / 60) for a, b in zip(hist, hist[1:])]
            rows.append(_section(f"{active_name} · {active['documents']:,} DOCS · "
                                 f"{active['data_mb'] + active['index_mb']:.1f} MB"))
            rows.append(_line(("ingest", Text(f"{rates[-1]:,.0f} docs/min" if rates else "measuring ...", style=OK)),
                              ("trend", _spark(rates, lo=0, hi=max(rates or [1]) * 1.1, width=30))))
            t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {LBL}", border_style=DIM, padding=(0, 1),
                      pad_edge=False, expand=True, show_edge=False)
            t.add_column("COLLECTION", no_wrap=True, ratio=2)
            t.add_column("DOCUMENTS", justify="right", ratio=1)
            t.add_column("SINCE OPEN", justify="right", ratio=1)
            t.add_column("SHARE", ratio=2)
            t.add_column("BUFFERED", justify="right", ratio=1)
            total = max(1, active["documents"])
            for coll, n in sorted(active["collections"].items(), key=lambda kv: -kv[1]):
                base = prev.setdefault(f"{active_name}.{coll}", n)
                delta = n - base
                t.add_row(Text(coll, style=VAL), f"{n:,}", Text(f"+{delta:,}" if delta else "·",
                                                               style=OK if delta else DIM),
                          _bar(n / total, 14, 2, 2), Text(str(pending.get(coll, "")), style=WARN))
            rows.append(t)
        if not stats.get("ok"):
            rows.append(Text(f"⚠ statistics stale: {stats.get('error', '')[:80]}", style=BAD))
        sub = f"updated {_ago(stats.get('updated_at'))}"
    else:
        rows.append(Text("waiting for MongoDB statistics ...", style=WARN))
        sub = None
    return _panel(Group(*rows), "3 · MONGODB DATABASE", sub)


def panel_backup(cfg: Settings, plat: dict | None) -> Panel:
    from .backup import list_backups
    rows: list = [_section("DAILY BACKUP · LOCAL")]
    bk = ((plat or {}).get("subsystems") or {}).get("backup") or {}
    root = cfg.backup_path
    free = sysinfo.disk(str(root if root.exists() else Path(cfg.data_dir))).get("free")
    nxt = bk.get("next_run")
    when = "-"
    if nxt == "now":
        when = "now"
    elif nxt:
        try:
            when = f"{nxt[11:16]}  (in {_dur((datetime.fromisoformat(nxt) - datetime.now().astimezone()).total_seconds())})"
        except ValueError:
            when = nxt
    rows.append(_line(("next", when), ("keep", f"{cfg.backup_keep_days} days"),
                      ("state", Text("RUNNING", style=WARN) if bk.get("running") else Text("idle", style=VAL)),
                      ("free", _bytes(free))))
    if bk.get("last_error"):
        rows.append(Text(f"  ⚠ last error: {bk['last_error'][:80]}", style=BAD))
    backups = list_backups(cfg)
    if backups:
        t = Table(box=box.SIMPLE_HEAD, header_style=f"bold {LBL}", border_style=DIM, padding=(0, 1),
                  pad_edge=False, expand=True, show_edge=False)
        for col, j in (("BACKUP (UTC)", "left"), ("TRIGGER", "left"), ("DOCS", "right"), ("SIZE", "right"),
                       ("TOOK", "right")):
            t.add_column(col, justify=j, no_wrap=True)
        for b in backups[:6]:
            t.add_row(Text(_stamp(b["name"]), style=OK if b.get("ok") else BAD), Text(b.get("trigger") or "", style=LBL),
                      f"{b.get('documents') or 0:,}", _bytes(b.get("bytes")), f"{_num(b.get('duration_s'), '.0f')} s")
        rows.append(t)
        total = sum(b.get("bytes") or 0 for b in backups)
        rows.append(Text(f"  {len(backups)} on disk · {_bytes(total)} · unchanged collections hard-linked", style=DIM))
    else:
        rows.append(Text("  no backup yet - first one at the scheduled time, or press b", style=WARN))

    rows.append(_section("CONFIGURATION"))
    t = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False, expand=True)
    t.add_column(style=LBL, no_wrap=True)
    t.add_column(style=VAL, ratio=1)
    t.add_column(style=LBL, no_wrap=True)
    t.add_column(style=VAL, ratio=1)
    chans = " ".join(f"{c.name}" for c in cfg.channels)
    pairs = [("station", cfg.station_id), ("database", cfg.mongodb_db),
             ("dataset", cfg.dataset_version), ("source", cfg.source),
             ("sdr exp", cfg.experiment_id), ("channels", chans),
             ("relay exp", cfg.relay_experiment_id), ("ir exp", cfg.ir_experiment_id),
             ("archive", str(cfg.archive_path)), ("relay data", str(cfg.relay_data_dir))]
    for i in range(0, len(pairs), 2):
        (a, b), (c, d) = pairs[i], pairs[i + 1]
        t.add_row(a, b, c, d)
    rows.append(t)
    subs = ((plat or {}).get("subsystems") or {})
    flags = Text()
    for n in ("sdr", "relay", "ir", "uploader", "stats", "backup"):
        if not (subs.get(n) or {}).get("enabled", True):
            continue
        alive = (subs.get(n) or {}).get("alive")
        flags.append(f"{'●' if alive else '○'} {n}   ", style=OK if alive else BAD)
    rows.append(_line(("threads", flags)))
    return _panel(Group(*rows), "4 · BACKUPS & CONFIG", f"v{__version__}")


def header(cfg: Settings, plat: dict | None, svc: str, states: list[dict] | None = None) -> Text:
    t = Text()
    t.append(" ▓▒░ FRE // DETECTION + MITIGATION ░▒▓ ", style="bold #001a08 on #39ff6a")
    t.append("  ")
    t.append_text(chips(states or []))
    t.append(f"  {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC", style=VAL)
    t.append(f"   service {svc}", style=LBL)
    return t


def render(cfg: Settings, prev: dict) -> Layout:
    d = Path(cfg.data_dir)
    plat = _load(d / "platform.json")
    sysi = sysinfo.snapshot()
    states = health(cfg, sysi)
    warn = alerts(states)
    layout = Layout()
    parts = [Layout(name="head", size=1)]
    if warn is not None:
        parts.append(Layout(name="alert", size=1))
    parts += [Layout(name="body"), Layout(name="foot", size=1)]
    layout.split_column(*parts)
    layout["body"].split_column(Layout(name="top"), Layout(name="bottom"))
    live = _load(d / "live.json")
    update_history(prev, live)
    layout["top"].split_row(Layout(panel_sdr(live, _load(d / "status.json"), sysi, prev.get("_snr"))),
                            Layout(panel_fre(_load(d / "relay_live.json"), _load(d / "ir_live.json"))))
    layout["bottom"].split_row(Layout(panel_mongo(_load(d / "mongo_stats.json"), plat, prev)),
                               Layout(panel_backup(cfg, plat)))
    layout["head"].update(header(cfg, plat, sysinfo.service_state(), states))
    if warn is not None:
        layout["alert"].update(warn)
    layout["foot"].update(Text(" Ctrl+C exit (collection keeps running) · fre = interactive app · "
                               f"state {d} · logs {cfg.log_dir or d / 'logs'}", style=DIM))
    return layout


def run_dashboard(cfg: Settings, stop=None, interval: float = 1.0) -> int:
    prev: dict = {}
    try:
        with Live(render(cfg, prev), screen=True, refresh_per_second=4, transient=False) as live:
            while stop is None or not stop.is_set():
                time.sleep(interval)
                live.update(render(cfg, prev))
    except KeyboardInterrupt:
        pass
    return 0
