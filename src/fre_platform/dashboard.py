"""Four-panel terminal dashboard (rich) for the integrated FRE platform.

    ┌──────────────────────────────┬──────────────────────────────┐
    │ [1] RPi // SDR live          │ [2] FRE // ESP32 relays + IR │
    │  reception, Pi health,       │  K1..K4 positions, events,   │
    │  connections                 │  optical-port probes         │
    ├──────────────────────────────┼──────────────────────────────┤
    │ [3] MongoDB // live counts   │ [4] Backups & config         │
    └──────────────────────────────┴──────────────────────────────┘

Reads only the JSON state files the service publishes in ``data_dir`` plus
local system information, so it can be opened and closed at any time without
touching collection.
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
from rich.table import Table
from rich.text import Text

from . import __version__, sysinfo
from .config import Settings

G, G2, DIM, WARN, BAD, HI = "green", "bright_green", "dark_green", "yellow", "bold red", "bold bright_green"
BORDER = "green"


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


def _ago(iso: str | None) -> str:
    a = _age_s(iso)
    if a is None:
        return "never"
    if a < 90:
        return f"{a:.0f}s ago"
    if a < 5400:
        return f"{a / 60:.0f}m ago"
    if a < 172800:
        return f"{a / 3600:.1f}h ago"
    return f"{a / 86400:.0f}d ago"


def _bytes(n: float | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "-"


def _bar(frac: float, width: int = 18, warn: float = 0.75, bad: float = 0.9) -> Text:
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    style = BAD if frac >= bad else WARN if frac >= warn else G2
    return Text("█" * n, style=style) + Text("░" * (width - n), style=DIM)


SPARK = "▁▂▃▄▅▆▇█"


def _spark(values, lo: float = -10.0, hi: float = 45.0, width: int = 24) -> Text:
    vals = [v for v in list(values)[-width:] if isinstance(v, (int, float))]
    if not vals:
        return Text(" " * width)
    chars = "".join(SPARK[max(0, min(7, int((v - lo) / (hi - lo) * 7.999)))] for v in vals)
    return Text(chars.rjust(width), style=G2)


def update_history(prev: dict, live: dict | None, maxlen: int = 120) -> None:
    """Remember per-station SNR of every new SDR block (for the sparklines)."""
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


def _num(v, fmt: str) -> str:
    return format(v, fmt) if isinstance(v, (int, float)) else "-"


def _kv(*pairs, style=G) -> Text:
    t = Text()
    for i, (k, v) in enumerate(pairs):
        if i:
            t.append("  ")
        t.append(f"{k} ", style=DIM)
        t.append_text(v if isinstance(v, Text) else Text(str(v), style=style))
    return t


def _ok(flag, yes="ONLINE", no="OFFLINE") -> Text:
    return Text(yes, style=HI) if flag else Text(no, style=BAD)


def _section(title: str) -> Text:
    return Text(f"▌{title}", style="bold bright_green")


def _panel(body, title: str) -> Panel:
    return Panel(body, title=f"[bold bright_green]{title}[/]", title_align="left", border_style=BORDER,
                 box=box.HEAVY, padding=(0, 1), style="green on black")


# ------------------------------------------------------------------ panels
def panel_sdr(live: dict | None, status: dict | None, sysi: dict, history: dict | None = None) -> Panel:
    rows: list = [_section("SDR // Airspy HF+ reception")]
    if live:
        age = _age_s(live.get("ts"))
        fresh = age is not None and age < 15
        rows.append(_kv(("block", f"#{live['seq']}"),
                        ("data", Text(_ago(live.get("ts")), style=G2 if fresh else BAD)),
                        ("capture", Text("OK", style=HI) if live.get("valid") else Text("INVALID", style=BAD))))
        rows.append(_kv(("floor", f"{_num(live.get('band_floor_db'), '.1f')} dBFS/Hz"),
                        ("power", f"{_num(live.get('band_power_dbfs'), '.1f')} dBFS")))
        t = Table(box=None, show_header=True, header_style=DIM, pad_edge=False, padding=(0, 1), expand=False)
        for col in ("stn", "kHz", "SNR", "", "SNR trend (1 s/char)", "state", "FSK", "last telegram"):
            t.add_column(col, style=G, no_wrap=True)
        for c in live.get("channels", []):
            recv = c.get("signal_state") == "present"
            snr = c.get("snr_db")
            frac = (snr + 20) / 60 if isinstance(snr, (int, float)) else 0
            last = ""
            for tg in c.get("telegrams", []):
                last = f"{tg['kind']}:{tg.get('assessment') or '?'}"
            lt = Text(last, style=BAD if "implausible" in last or "integrity" in last else G2)
            t.add_row(c["name"], f"{c['freq_hz'] / 1e3:.1f}", _num(snr, "5.1f"), _bar(frac, 10, 2, 2),
                      _spark((history or {}).get(c["name"], []), width=24),
                      Text("● RECV", style=HI) if recv else Text("○ ----", style=DIM),
                      _num(c.get("fsk_activity"), ".2f"), lt)
        rows.append(t)
        tot = live.get("telegram_totals") or {}
        rows.append(_kv(("telegrams", f"{tot.get('total', 0)}"), ("time", f"{tot.get('time', 0)}"),
                        ("suspect", Text(str(tot.get("suspect", 0)), style=BAD if tot.get("suspect") else G))))
    else:
        rows.append(Text("waiting for SDR collector ... (live.json missing)", style=WARN))
    if status:
        src = status.get("source_stats") or {}
        rows.append(_kv(("restarts", src.get("restarts", 0)), ("dropped", src.get("dropped_blocks", 0)),
                        ("since", _ago((status.get("collector") or {}).get("started_at")))))

    rows.append(_section("RPi // system"))
    mem, dsk = sysi["mem"], sysi["disk"]
    temp = sysi.get("temp_c")
    rows.append(_kv(("host", sysi["host"]), ("up", f"{sysi['uptime_s'] / 86400:.1f}d"),
                    ("temp", Text(f"{_num(temp, '.0f')}°C", style=BAD if (temp or 0) > 75 else G)),
                    ("load", " ".join(f"{x:.2f}" for x in sysi["load"]))))
    rows.append(_kv(("mem ", _bar(mem["pct"] / 100, 12)), ("", f"{mem['pct']:.0f}%"),
                    ("disk", _bar(dsk["pct"] / 100, 12)), ("", f"{dsk['pct']:.0f}% ({_bytes(dsk['free'])} free)")))
    if sysi.get("throttled") not in (None, "ok"):
        rows.append(Text(f"power/thermal: {sysi['throttled']}", style=BAD))
    usb = sysi.get("usb") or {}
    rows.append(_kv(*[(name, Text("✔", style=HI) if usb.get(key) else Text("✘", style=BAD))
                      for name, key in (("airspy", "airspy"), ("esp32", "esp32"), ("ir-head", "ir_head"))]))
    rows.append(_kv(*[(k, v) for k, v in (sysi.get("addresses") or {}).items()]))

    conn = sysi.get("connections") or {}
    rows.append(_section(f"NET // {conn.get('total', 0)} established connections"))
    rows.append(_kv(*[(k, v) for k, v in sorted((conn.get("by_kind") or {}).items())]))
    seen = set()
    order = {"ssh-in": 0, "remote-desktop": 1, "tailscale": 2, "mongodb": 3, "ssh-out": 4}
    for r in sorted(conn.get("rows", []), key=lambda r: order.get(r["kind"], 9)):
        if r["kind"] in ("local",) or (r["kind"], r["peer"].rsplit(":", 1)[0]) in seen:
            continue
        seen.add((r["kind"], r["peer"].rsplit(":", 1)[0]))
        rows.append(Text(f"  {r['kind']:<9} {r['peer']:<28} {r['proc']}", style=G))
        if len(seen) >= 6:
            break
    for who in (sysi.get("logins") or [])[:3]:
        rows.append(Text(f"  login     {who}", style=DIM))
    return _panel(Group(*rows), "[1] RPi // SDR LIVE")


POS = {"a": ("a", "active"), "b": ("b", "rest")}


def panel_fre(relay: dict | None, ir: dict | None) -> Panel:
    rows: list = [_section("FRE ESP32 // relay contacts K1..K4")]
    if relay:
        line_age = _age_s(relay.get("last_line_ts"))
        alive = relay.get("connected") and line_age is not None and line_age < 120
        rows.append(_kv(("esp32", _ok(relay.get("connected"), "CONNECTED", "DISCONNECTED")),
                        ("fw", relay.get("fw") or "?"),
                        ("last line", Text(_ago(relay.get("last_line_ts")), style=G2 if alive else WARN))))
        if relay.get("error") and not relay.get("connected"):
            rows.append(Text(f"  {relay['error']}", style=BAD))
        if relay.get("in_gap"):
            rows.append(Text("  ⚠ ESP silent (heartbeat missing)", style=BAD))
        t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style=DIM, padding=(0, 1), expand=False,
                  border_style=DIM)
        for col in ("relay", "level", "pos", "meaning"):
            t.add_column(col, style=G, no_wrap=True)
        levels = {"K1": "100 %", "K2": "60 %", "K3": "30 %", "K4": "0 %"}
        for r in ("K1", "K2", "K3", "K4"):
            p = (relay.get("positions") or {}).get(r)
            code, meaning = POS.get(p, ("?", "unknown"))
            t.add_row(r, levels[r], Text(code, style=HI if p == "a" else G),
                      Text(meaning, style=HI if p == "a" else DIM))
        rows.append(t)
        lvl = relay.get("level_pct")
        rows.append(_kv(("requested feed-in", Text(f"{lvl} %" if lvl is not None else "ambiguous (all a / switching)",
                                                   style=HI if lvl is not None else WARN)),
                        ("events today", relay.get("events_today", 0))))
        rows.append(_kv(("last event", _ago(relay.get("last_event_ts"))), ("boots", relay.get("boots", 0)),
                        ("port", (relay.get("port") or "-").split("/")[-1][:28])))
        for ev in (relay.get("recent") or [])[:5]:
            ts = (ev.get("ts") or "")[5:19].replace("T", " ")
            if ev.get("type") == "event":
                txt = f"  {ts}  {ev['relay']} -> {ev['position']} ({ev.get('level_pct')} %)"
                rows.append(Text(txt, style=HI))
            else:
                rows.append(Text(f"  {ts}  {ev['type']} {ev.get('detail') or ''}"[:70], style=DIM))
    else:
        rows.append(Text("relay monitor not running (relay_live.json missing)", style=WARN))

    rows.append(_section("FRE IR // optical port IEC 62056-21 (read-only)"))
    if ir:
        rows.append(_kv(("head", Text("PRESENT", style=HI) if ir.get("head_present")
                         else Text("NOT CONNECTED", style=WARN)),
                        ("echo", Text("ok", style=G2) if ir.get("echo_ok") else Text("-", style=DIM)),
                        ("probes", ir.get("probes", 0)),
                        ("answers", Text(str(ir.get("responses", 0)), style=HI if ir.get("responses") else G))))
        rows.append(_kv(("last probe", _ago(ir.get("last_probe_ts"))),
                        ("next", (ir.get("next_probe_ts") or "-")[11:19] or "-"),
                        ("every", f"{ir.get('interval_s', 0):.0f}s")))
        if ir.get("last_ident"):
            rows.append(Text(f"  ident {ir['last_ident']}", style=HI))
            for line in (ir.get("last_lines") or [])[:6]:
                rows.append(Text(f"    {line}", style=G2))
        elif ir.get("probes"):
            rows.append(Text("  FRE has not answered on the optical port (no identification)", style=WARN))
        if ir.get("last_error"):
            rows.append(Text(f"  {ir['last_error']}", style=BAD))
    else:
        rows.append(Text("IR probe not running (ir_live.json missing)", style=WARN))
    return _panel(Group(*rows), "[2] FRE // ESP32 + IR LIVE")


def panel_mongo(stats: dict | None, plat: dict | None, prev: dict) -> Panel:
    rows: list = []
    up = ((plat or {}).get("subsystems") or {}).get("uploader") or {}
    buf = (plat or {}).get("buffer") or {}
    rows.append(_kv(("atlas", _ok(up.get("online"))), ("uploaded", up.get("uploaded", 0)),
                    ("last ok", _ago(up.get("last_success"))),
                    ("latency", f"{_num((stats or {}).get('latency_ms'), '.0f')} ms")))
    rows.append(_kv(("buffer", Text(str(buf.get("rows", 0)), style=WARN if buf.get("rows", 0) > 500 else G)),
                    ("dead-letter", Text(str(buf.get("dead_letter", 0)), style=BAD if buf.get("dead_letter") else G)),
                    ("rejected", up.get("rejected", 0)), ("failures", up.get("failures", 0))))
    if up.get("last_error") and not up.get("online"):
        rows.append(Text(f"  {up['last_error'][:90]}", style=BAD))
    if stats:
        used, quota = stats.get("cluster_used_mb", 0), stats.get("quota_mb", 512)
        rows.append(_kv(("cluster", _bar(used / quota if quota else 0, 20, 0.7, stats.get("soft_limit_pct", 85) / 100)),
                        ("", f"{used:.0f} / {quota:.0f} MB"),
                        ("", Text("QUOTA GUARD: bulk data held back", style=BAD) if up.get("quota_restricted") else "")))
        pending = buf.get("by_collection") or {}
        dbs = stats.get("databases") or {}
        active = next((d for d in dbs.values() if d.get("active")), None)
        if active:
            t0 = prev.setdefault("_rate_t0", (time.time(), active["documents"]))
            mins = (time.time() - t0[0]) / 60
            if mins >= 1:
                rows.append(_kv(("ingest", Text(f"{(active['documents'] - t0[1]) / mins:,.1f} docs/min", style=HI)),
                                ("over", f"{mins:.0f} min")))
        for name in sorted(dbs, key=lambda n: (not dbs[n].get("active"), n)):
            d = dbs[name]
            tag = Text(" ◄ ACTIVE", style=HI) if d.get("active") else Text(" legacy (frozen)", style=DIM)
            rows.append(Text(f"▌{name}", style="bold bright_green") + tag
                        + Text(f"   {d['documents']:,} docs  {d['data_mb'] + d['index_mb']:.1f} MB", style=G))
            t = Table(box=None, show_header=d.get("active", False), header_style=DIM, padding=(0, 1),
                      pad_edge=False)
            t.add_column("collection", style=G, no_wrap=True, min_width=14)
            t.add_column("documents", style=G2, justify="right")
            t.add_column("+since open", style=HI, justify="right")
            t.add_column("buffered", style=WARN, justify="right")
            for coll, n in d["collections"].items():
                base = prev.setdefault(f"{name}.{coll}", n)
                delta = n - base
                t.add_row(coll, f"{n:,}", f"+{delta:,}" if delta else "",
                          str(pending.get(coll, "")) if d.get("active") else "")
            rows.append(t)
        if not stats.get("ok"):
            rows.append(Text(f"stats stale: {stats.get('error', '')[:80]}", style=BAD))
        rows.append(Text(f"updated {_ago(stats.get('updated_at'))}", style=DIM))
    else:
        rows.append(Text("waiting for MongoDB statistics (mongo_stats.json missing) ...", style=WARN))
    return _panel(Group(*rows), "[3] MongoDB // LIVE COUNTS")


def panel_backup(cfg: Settings, plat: dict | None) -> Panel:
    from .backup import list_backups
    rows: list = []
    bk = ((plat or {}).get("subsystems") or {}).get("backup") or {}
    root = cfg.backup_path
    free = sysinfo.disk(str(root if root.exists() else Path(cfg.data_dir))).get("free")
    rows.append(_kv(("daily at", cfg.backup_time), ("next", (bk.get("next_run") or "-").replace("T", " ")[:16]),
                    ("keep", f"{cfg.backup_keep_days} d"),
                    ("state", Text("RUNNING", style=WARN) if bk.get("running") else Text("idle", style=G))))
    rows.append(_kv(("dir", str(root)), ("free", _bytes(free))))
    if bk.get("last_error"):
        rows.append(Text(f"  last error: {bk['last_error'][:80]}", style=BAD))
    t = Table(box=box.SIMPLE_HEAD, header_style=DIM, padding=(0, 1), border_style=DIM)
    for col, j in (("backup (UTC)", "left"), ("documents", "right"), ("size", "right"), ("took", "right"),
                   ("dbs", "left")):
        t.add_column(col, style=G, justify=j, no_wrap=True)
    backups = list_backups(cfg)
    for b in backups[:7]:
        t.add_row(Text(_stamp(b["name"]), style=HI if b.get("ok") else BAD), f"{b.get('documents') or 0:,}",
                  _bytes(b.get("bytes")), f"{_num(b.get('duration_s'), '.0f')}s", ",".join(b["databases"]))
    if backups:
        rows.append(t)
        total = sum(b.get("bytes") or 0 for b in backups)
        rows.append(Text(f"  {len(backups)} backups on disk, {_bytes(total)} (unchanged collections are hard-linked)",
                         style=DIM))
    else:
        rows.append(Text("  no backup yet - first one runs at the scheduled time (or: main.py backup)", style=WARN))

    rows.append(_section("CONFIG"))
    chans = " ".join(f"{c.name}:{c.freq_hz / 1e3:g}k" for c in cfg.channels)
    rows.append(_kv(("station", cfg.station_id), ("db", cfg.mongodb_db)))
    rows.append(_kv(("dataset", cfg.dataset_version), ("sdr exp", cfg.experiment_id)))
    rows.append(_kv(("relay exp", cfg.relay_experiment_id), ("ir exp", cfg.ir_experiment_id)))
    rows.append(_kv(("source", cfg.source), ("channels", chans)))
    rows.append(_kv(("relay data", str(cfg.relay_data_dir)), ("ir every", f"{cfg.ir_probe_interval_s:.0f}s")))
    rows.append(_kv(("data", str(cfg.data_dir)), ("logs", str(cfg.log_dir or "-"))))
    subs = ((plat or {}).get("subsystems") or {})
    flags = [(n, Text("●", style=HI) if (subs.get(n) or {}).get("alive") else Text("○", style=BAD))
             for n in ("sdr", "relay", "ir", "uploader", "stats", "backup") if (subs.get(n) or {}).get("enabled", True)]
    rows.append(_kv(("threads", ""), *flags))
    return _panel(Group(*rows), "[4] BACKUPS & CONFIG")


def header(cfg: Settings, plat: dict | None, svc: str) -> Text:
    now = datetime.now().astimezone()
    age = _age_s((plat or {}).get("updated_at"))
    alive = age is not None and age < 20
    t = Text()
    t.append(" ▓▒░ FRE//DETECTION+MITIGATION ░▒▓ ", style="bold black on bright_green")
    t.append(f"  v{__version__}  ", style=DIM)
    t.append(f"{now:%Y-%m-%d %H:%M:%S %Z}  ", style=G2)
    t.append(f"UTC {datetime.now(timezone.utc):%H:%M:%S}  ", style=G)
    t.append("service ", style=DIM)
    t.append(svc.upper() if svc != "unknown" else "FOREGROUND", style=HI if svc == "active" else WARN)
    t.append("  core ", style=DIM)
    t.append("ALIVE" if alive else "NO HEARTBEAT", style=HI if alive else BAD)
    t.append("   receive-only · nothing is transmitted", style=DIM)
    return t


def render(cfg: Settings, prev: dict) -> Layout:
    d = Path(cfg.data_dir)
    plat = _load(d / "platform.json")
    sysi = sysinfo.snapshot()
    layout = Layout()
    layout.split_column(Layout(name="head", size=1), Layout(name="body"),
                        Layout(name="foot", size=1))
    layout["body"].split_column(Layout(name="top"), Layout(name="bottom"))
    live = _load(d / "live.json")
    update_history(prev, live)
    layout["top"].split_row(Layout(panel_sdr(live, _load(d / "status.json"), sysi, prev.get("_snr"))),
                            Layout(panel_fre(_load(d / "relay_live.json"), _load(d / "ir_live.json"))))
    layout["bottom"].split_row(Layout(panel_mongo(_load(d / "mongo_stats.json"), plat, prev)),
                               Layout(panel_backup(cfg, plat)))
    layout["head"].update(header(cfg, plat, sysinfo.service_state()))
    layout["foot"].update(Text(" Ctrl+C exit (collection keeps running)  ·  main.py --help  ·  "
                               f"state: {d}  ·  logs: {cfg.log_dir or d / 'logs'}", style=DIM))
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
