"""FRE Platform - interactive terminal application (Textual).

    fre                      start / attach: makes sure the service runs, then opens this app

Tabs:  1 Overview (4 panels) · 2 Timeline · 3 Telegrams · 4 Relays · 5 Logs · 6 Backups & archive
Keys:  z zoom a panel · b backup now · p IR probe now · u upload now · R restart service · q quit

The app only *reads* the state files the platform publishes and sends requests
through the command inbox (:mod:`fre_platform.control`); collection continues
when the app is closed.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Label, RichLog, Static, TabbedContent,
                             TabPane)

from . import __version__, control, dashboard, sysinfo
from .config import Settings

CSS = """
Screen { background: #000000; color: #41ff7f; }
#topbar { dock: top; height: 1; background: #04140a; }
#alertbar { dock: top; height: 1; background: #2a0a0a; display: none; }
#alertbar.show { display: block; }
Footer { background: #04140a; color: #41ff7f; }
FooterKey { background: #04140a; color: #7dff9b; }
FooterKey .footer-key--key { background: #1fa64a; color: #001a08; text-style: bold; }
TabbedContent { background: #000000; }
Tabs { background: #000000; height: 2; }
Tab { color: #46b46c; background: #000000; padding: 0 2; }
Tab:hover { color: #d2ffdd; background: #0b2a15; }
Tab.-active { color: #001a08; background: #39ff6a; text-style: bold; }
Underline > .underline--bar { color: #39ff6a; background: #0b2a15; }
TabPane { padding: 0; background: #000000; }
#overview { grid-size: 2 2; grid-gutter: 0 1; height: 1fr; }
#overview Static { height: 1fr; overflow: hidden; background: #000000; }
#overview.zoomed { grid-size: 1 1; }
#overview.zoomed Static { display: none; }
#overview.zoomed Static.zoom { display: block; }
DataTable { background: #000000; color: #41ff7f; height: 1fr; scrollbar-color: #1fa64a;
            scrollbar-background: #04140a; }
DataTable > .datatable--header { background: #0b2a15; color: #7dff9b; text-style: bold; }
DataTable > .datatable--cursor { background: #1fa64a; color: #001a08; text-style: bold; }
DataTable > .datatable--hover { background: #0b2a15; }
DataTable > .datatable--even-row { background: #020d05; }
RichLog { background: #000000; color: #41ff7f; scrollbar-color: #1fa64a; scrollbar-background: #04140a;
          border: round #1fa64a; }
.bar { height: auto; max-height: 12; padding: 0 1; border: round #1fa64a; background: #000000; }
.hint { color: #46b46c; padding: 0 1; height: 1; }
ConfirmScreen { align: center middle; background: rgba(0, 0, 0, 0.75); }
#dialog { width: 64; height: auto; border: round #39ff6a; background: #000000; padding: 1 2; }
#dialog Label { width: 100%; content-align: center middle; color: #d2ffdd; margin-bottom: 1; }
#dialog Horizontal { height: auto; align: center middle; }
Button { margin: 0 2; background: #0b2a15; color: #7dff9b; border: tall #1fa64a; }
Button:hover { background: #1fa64a; color: #001a08; }
Button.-error { background: #3b0000; color: #ff5555; border: tall #aa0000; }
"""

LEVEL_STYLE = {"DEBUG": "#46b46c", "INFO": "#41ff7f", "WARNING": "bold #ffd166", "ERROR": "bold #ff5555",
               "CRITICAL": "bold #ffffff on #c0392b"}
ASSESS_STYLE = {"authentic_candidate": "#7dff9b", "implausible": "bold #ff5555", "integrity_fail": "bold #ff5555",
                "unverified": "bold #ffd166"}


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, question: str, danger: bool = False):
        super().__init__()
        self.question, self.danger = question, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            with Horizontal():
                yield Button("Yes", variant="error" if self.danger else "primary", id="yes")
                yield Button("No", id="no")

    @on(Button.Pressed)
    def _answer(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")


class FreApp(App):
    TITLE = "FRE // DETECTION + MITIGATION"
    CSS = CSS
    BINDINGS = [
        Binding("1", "tab('overview-tab')", "Overview"),
        Binding("2", "tab('timeline-tab')", "Timeline"),
        Binding("3", "tab('telegrams-tab')", "Telegrams"),
        Binding("4", "tab('relays-tab')", "Relays"),
        Binding("5", "tab('logs-tab')", "Logs"),
        Binding("6", "tab('backups-tab')", "Backups"),
        Binding("z", "zoom", "Zoom panel"),
        Binding("escape", "unzoom", "Unzoom", show=False),
        Binding("b", "backup", "Backup now"),
        Binding("p", "ir_probe", "IR probe"),
        Binding("u", "flush", "Upload now"),
        Binding("R", "restart", "Restart service"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Settings, mode: str = "service", platform=None):
        super().__init__()
        self.cfg = cfg
        self.mode = mode            # "service" (attached to systemd) or "foreground" (we run the platform)
        self.platform = platform
        self.data = Path(cfg.data_dir)
        self.prev_counts: dict = {}
        self._tg_stamp = None
        self._relay_stamp = None
        self._backup_stamp = None
        self._log_pos = 0
        self._log_inode = None
        self._timeline_stamp = None
        self.zoom = 0

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        yield Static(id="alertbar")
        with TabbedContent(initial="overview-tab"):
            with TabPane("1 OVERVIEW", id="overview-tab"):
                with Grid(id="overview"):
                    yield Static(id="p-sdr")
                    yield Static(id="p-fre")
                    yield Static(id="p-mongo")
                    yield Static(id="p-backup")
            with TabPane("2 TIMELINE", id="timeline-tab"):
                yield Static(classes="hint", id="tl-hint")
                yield DataTable(id="tl-table", zebra_stripes=True, cursor_type="row")
            with TabPane("3 TELEGRAMS", id="telegrams-tab"):
                yield Static(classes="hint", id="tg-hint")
                yield DataTable(id="tg-table", zebra_stripes=True, cursor_type="row")
            with TabPane("4 RELAYS", id="relays-tab"):
                yield Static(classes="bar", id="relay-now")
                yield DataTable(id="relay-table", zebra_stripes=True, cursor_type="row")
            with TabPane("5 LOGS", id="logs-tab"):
                yield RichLog(id="log", max_lines=3000, wrap=False, highlight=False, markup=False)
            with TabPane("6 BACKUPS", id="backups-tab"):
                yield Static(classes="bar", id="backup-info")
                yield DataTable(id="backup-table", zebra_stripes=True, cursor_type="row")
                yield Static(classes="bar", id="archive-info")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"v{__version__} · {self.cfg.station_id} · db {self.cfg.mongodb_db} · " \
                         f"{'service' if self.mode == 'service' else 'foreground run'}"
        self.query_one("#tl-table", DataTable).add_columns(
            "time (UTC)", "source", "event", "details", "relay reaction to last command telegram")
        self.query_one("#tg-table", DataTable).add_columns(
            "received (UTC)", "station", "kind", "assessment", "clock Δ s", "decoded time", "EVU", "chk",
            "SNR dB", "raw")
        self.query_one("#relay-table", DataTable).add_columns(
            "time (UTC)", "type", "relay", "→ pos", "level", "Δ ms", "detail")
        self.query_one("#backup-table", DataTable).add_columns(
            "backup (UTC timestamp)", "trigger", "status", "documents", "size", "took", "databases", "folder")
        self.refresh_overview()
        self.refresh_tables()
        self.tail_logs()
        self.set_interval(1.0, self.refresh_overview)
        self.set_interval(3.0, self.refresh_tables)
        self.set_interval(1.0, self.tail_logs)

    # ---------------------------------------------------------- overview
    def refresh_overview(self) -> None:
        d = self.data
        plat = _load(d / "platform.json")
        sysi = sysinfo.snapshot()
        live = _load(d / "live.json")
        dashboard.update_history(self.prev_counts, live)
        panels = {
            "#p-sdr": dashboard.panel_sdr(live, _load(d / "status.json"), sysi, self.prev_counts.get("_snr")),
            "#p-fre": dashboard.panel_fre(_load(d / "relay_live.json"), _load(d / "ir_live.json")),
            "#p-mongo": dashboard.panel_mongo(_load(d / "mongo_stats.json"), plat, self.prev_counts),
            "#p-backup": dashboard.panel_backup(self.cfg, plat),
        }
        for sel, panel in panels.items():
            w = self.query_one(sel, Static)
            if w.size.height:
                panel.height = w.size.height
            w.update(panel)
        svc = sysinfo.service_state() if self.mode == "service" else "foreground"
        states = dashboard.health(self.cfg, sysi)
        self.query_one("#topbar", Static).update(dashboard.header(self.cfg, plat, svc, states))
        warn = dashboard.alerts(states)
        bar = self.query_one("#alertbar", Static)
        bar.set_class(warn is not None, "show")
        if warn is not None:
            bar.update(warn)

    # ------------------------------------------------------------ tables
    def refresh_tables(self) -> None:
        self._refresh_timeline()
        self._refresh_telegrams()
        self._refresh_relays()
        self._refresh_backups()

    def _refresh_telegrams(self) -> None:
        data = _load(self.data / "telegrams_recent.json") or {}
        stamp = data.get("updated_at")
        tgs = data.get("telegrams") or []
        live = _load(self.data / "live.json") or {}
        tot = live.get("telegram_totals") or {}
        self.query_one("#tg-hint", Static).update(Text(
            f"latest {len(tgs)} received telegrams (newest first) · since start: {tot.get('total', 0)} total, "
            f"{tot.get('time', 0)} time, {tot.get('suspect', 0)} suspect · "
            "time telegram = German local time, compared with the Pi's NTP clock", style="#46b46c"))
        if stamp == self._tg_stamp:
            return
        self._tg_stamp = stamp
        t = self.query_one("#tg-table", DataTable)
        t.clear()
        for tg in tgs:
            a = tg.get("assessment") or "-"
            off = tg.get("clock_offset_s")
            t.add_row(tg.get("ts", "")[:19].replace("T", " "), tg.get("channel", ""), tg.get("kind", ""),
                      Text(a, style=ASSESS_STYLE.get(a, "#41ff7f")),
                      Text(f"{off:+.1f}" if isinstance(off, (int, float)) else "-",
                           style="bold #ff5555" if isinstance(off, (int, float)) and abs(off) > 5 else "#41ff7f"),
                      (tg.get("time") or "-")[:19].replace("T", " "), tg.get("evu") or "-",
                      Text("ok", style="#7dff9b") if tg.get("checksum_ok") else Text("BAD", style="bold #ff5555"),
                      f"{tg['snr_db']:.1f}" if isinstance(tg.get("snr_db"), (int, float)) else "-",
                      (tg.get("raw_hex") or "")[:48])

    def _refresh_relays(self) -> None:
        live = _load(self.data / "relay_live.json") or {}
        pos = live.get("positions") or {}
        now = Text()
        now.append("ESP32 ", style="#46b46c")
        now.append("CONNECTED" if live.get("connected") else "DISCONNECTED",
                   style="bold #39ff6a" if live.get("connected") else "bold #ff5555")
        now.append(f"   fw {live.get('fw') or '?'}   last line {dashboard._ago(live.get('last_line_ts'))}   "
                   f"events today {live.get('events_today', 0)}\n", style="#41ff7f")
        for r, lvl in (("K1", "100 %"), ("K2", " 60 %"), ("K3", " 30 %"), ("K4", "  0 %")):
            p = pos.get(r)
            now.append(f" {r} {lvl} ", style="#41ff7f")
            now.append(f"[{'■ ACTIVE a' if p == 'a' else '□ rest   b' if p == 'b' else '?  unknown'}]",
                       style="bold #001a08 on #39ff6a" if p == "a" else "#41ff7f")
            now.append("   ")
        lvl = live.get("level_pct")
        now.append(f"\n requested feed-in: {f'{lvl} %' if lvl is not None else 'ambiguous (all a / switching)'}",
                   style="bold #39ff6a" if lvl is not None else "bold #ffd166")
        self.query_one("#relay-now", Static).update(now)

        files = sorted(Path(self.cfg.relay_data_dir).glob("relays_*.jsonl"))
        if not files:
            return
        f = files[-1]
        try:
            stamp = (f.name, f.stat().st_mtime)
        except OSError:
            return
        if stamp == self._relay_stamp:
            return
        self._relay_stamp = stamp
        rows = []
        try:
            for line in f.read_text(errors="ignore").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") not in ("state", "other"):  # heartbeats / ESP boot-ROM noise stay in the files
                    rows.append(r)
        except OSError:
            return
        t = self.query_one("#relay-table", DataTable)
        t.clear()
        prev_ms = None
        out = []
        for r in rows:  # oldest -> newest to compute switchover gaps
            ms = r.get("esp_ms") if r.get("type") == "event" else None
            gap = ms - prev_ms if isinstance(ms, int) and isinstance(prev_ms, int) and 0 <= ms - prev_ms <= 5000 else None
            if isinstance(ms, int):
                prev_ms = ms
            out.append((r, gap))
        for r, gap in reversed(out[-500:]):
            ev = r.get("type") == "event"
            style = "bold #39ff6a" if ev and r.get("position") == "a" else "#41ff7f" if ev else "#46b46c"
            t.add_row(Text(r.get("ts_utc", "")[:23].replace("T", " "), style=style), Text(r.get("type", ""), style=style),
                      r.get("relay") or "", Text(r.get("position") or "", style=style),
                      f"{r['level_pct']} %" if r.get("level_pct") is not None else "",
                      f"+{gap}" if gap is not None else "", (r.get("detail") or r.get("fw") or "")[:60])

    def _refresh_backups(self) -> None:
        from .backup import list_backups
        plat = _load(self.data / "platform.json") or {}
        bk = (plat.get("subsystems") or {}).get("backup") or {}
        info = Text()
        info.append("daily at ", style="#46b46c")
        info.append(f"{self.cfg.backup_time}", style="#7dff9b")
        info.append(f"   next {(bk.get('next_run') or '-').replace('T', ' ')[:16]}   keep {self.cfg.backup_keep_days} days"
                    f"   databases {', '.join(self.cfg.backup_db_list)}\n", style="#41ff7f")
        info.append(f"dir {self.cfg.backup_path}   ", style="#41ff7f")
        info.append("RUNNING ..." if bk.get("running") else "idle", style="bold #ffd166" if bk.get("running") else "#41ff7f")
        if bk.get("last_error"):
            info.append(f"   last error: {bk['last_error'][:90]}", style="bold #ff5555")
        info.append("\nrestore: zcat <day>/<db>/<collection>.jsonl.gz | mongoimport --uri \"$URI\" -d <db> -c <collection>"
                    "      press b to back up now", style="#46b46c")
        self.query_one("#backup-info", Static).update(info)
        self._refresh_archive()
        backups = list_backups(self.cfg)
        stamp = tuple((b["name"], b.get("finished_at")) for b in backups)
        if stamp == self._backup_stamp:
            return
        self._backup_stamp = stamp
        t = self.query_one("#backup-table", DataTable)
        t.clear()
        for b in backups:
            t.add_row(Text(dashboard._stamp(b["name"]), style="#7dff9b"), b.get("trigger") or "",
                      Text("OK", style="bold #39ff6a") if b.get("ok") else Text("FAILED", style="bold #ff5555"),
                      f"{b.get('documents') or 0:,}", dashboard._bytes(b.get("bytes")),
                      f"{b.get('duration_s') or 0:.0f} s", ", ".join(b["databases"]), b["name"])

    def _refresh_archive(self) -> None:
        from .archive import TimeArchive
        arc = TimeArchive(self.cfg.archive_path)
        t = Text()
        t.append("LOCAL ARCHIVE ", style="bold #39ff6a")
        t.append(f"{self.cfg.archive_path}/YYYY/MM/DD/<collection>.jsonl[.gz]   every record filed by its own UTC "
                 f"timestamp · keep {'forever' if not self.cfg.archive_keep_days else f'{self.cfg.archive_keep_days} d'}\n",
                 style="#41ff7f")
        days = arc.summary(days=5)
        if not days:
            t.append("  no archived records yet", style="bold #ffd166")
        for d in days:
            t.append(f"  {d['day']}  {dashboard._bytes(d['bytes']):>9}  ", style="#7dff9b")
            t.append(" ".join(f.replace(".jsonl", "") for f in d["files"]) + "\n", style="#46b46c")
        self.query_one("#archive-info", Static).update(t)

    # ---------------------------------------------------------- timeline
    @staticmethod
    def _parse_ts(v: str | None) -> datetime | None:
        try:
            return datetime.fromisoformat(v) if v else None
        except ValueError:
            return None

    def timeline_events(self) -> list[dict]:
        """Telegrams, relay events and IR probes merged and sorted by their UTC timestamp."""
        ev: list[dict] = []
        for tg in (_load(self.data / "telegrams_recent.json") or {}).get("telegrams", []):
            off = tg.get("clock_offset_s")
            ev.append({"ts": self._parse_ts(tg.get("ts")), "source": "SDR", "event": f"telegram {tg.get('kind')}",
                       "kind": tg.get("kind"), "style": "#7dff9b" if tg.get("kind") != "time" else "#46b46c",
                       "detail": f"{tg.get('channel')}  {tg.get('assessment') or '-'}"
                                 + (f"  clock {off:+.1f}s" if isinstance(off, (int, float)) else "")
                                 + (f"  evu {tg['evu']}" if tg.get("evu") else "")
                                 + f"  {(tg.get('raw_hex') or '')[:24]}"})
        files = sorted(Path(self.cfg.relay_data_dir).glob("relays_*.jsonl"))[-2:]
        for f in files:
            try:
                lines = f.read_text(errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("type") in ("state", "other"):
                    continue
                if r.get("type") == "event":
                    detail = f"{r.get('relay')} -> {r.get('position')}  ({r.get('level_pct')} %)  esp {r.get('esp_ms')} ms"
                else:
                    detail = r.get("detail") or r.get("fw") or ""
                ev.append({"ts": self._parse_ts(r.get("ts_utc")), "source": "RELAY", "event": r.get("type"),
                           "detail": detail, "style": "bold #39ff6a" if r.get("type") == "event" else "#41ff7f"})
        for pr in (_load(self.data / "ir_live.json") or {}).get("recent", []):
            ev.append({"ts": self._parse_ts(pr.get("ts")), "source": "IR",
                       "event": "ir answer" if pr.get("responded") else "ir probe",
                       "detail": pr.get("ident") or pr.get("error") or ("no answer, echo ok" if pr.get("echo") else "no answer"),
                       "style": "bold #39ff6a" if pr.get("responded") else "#46b46c"})
        ev = [e for e in ev if e["ts"] is not None]
        ev.sort(key=lambda e: e["ts"])
        last_cmd = None
        for e in ev:  # correlate: relay switch after the most recent command telegram
            if e["source"] == "SDR" and e.get("kind") not in ("time", None):
                last_cmd = e
            elif e["source"] == "RELAY" and e["event"] == "event" and last_cmd is not None:
                dt = (e["ts"] - last_cmd["ts"]).total_seconds()
                if 0 <= dt <= 300:
                    e["corr"] = f"+{dt:.1f} s after {last_cmd['event']} ({last_cmd['detail'].split('  ')[0]})"
        return ev

    def _refresh_timeline(self) -> None:
        ev = self.timeline_events()
        self.query_one("#tl-hint", Static).update(Text(
            f"{len(ev)} events from all sources, ordered by UTC timestamp (newest first) · relay switches are matched to "
            "the most recent command telegram within 300 s · same data as the MongoDB view 'timeline'",
            style="#46b46c"))
        stamp = (len(ev), ev[-1]["ts"] if ev else None)
        if stamp == self._timeline_stamp:
            return
        self._timeline_stamp = stamp
        t = self.query_one("#tl-table", DataTable)
        t.clear()
        for e in reversed(ev[-800:]):
            st = e["style"]
            t.add_row(Text(e["ts"].strftime("%Y-%m-%d %H:%M:%S.%f")[:23], style=st), Text(e["source"], style=st),
                      Text(e["event"] or "", style=st), Text(e["detail"][:110], style=st),
                      Text(e.get("corr", ""), style="bold #ffd166"))

    # ------------------------------------------------------------- zoom
    def action_zoom(self) -> None:
        if self.query_one(TabbedContent).active != "overview-tab":
            self.action_tab("overview-tab")
            return
        self.zoom = (self.zoom + 1) % 5
        self._apply_zoom()

    def action_unzoom(self) -> None:
        self.zoom = 0
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        grid = self.query_one("#overview", Grid)
        grid.set_class(self.zoom > 0, "zoomed")
        for i, sel in enumerate(("#p-sdr", "#p-fre", "#p-mongo", "#p-backup"), start=1):
            self.query_one(sel, Static).set_class(self.zoom == i, "zoom")
        self.refresh_overview()

    # -------------------------------------------------------------- logs
    def tail_logs(self) -> None:
        path = Path(self.cfg.log_dir or self.data / "logs") / "fre-platform.log"
        log = self.query_one("#log", RichLog)
        try:
            st = path.stat()
        except OSError:
            if self._log_pos == 0:
                log.write(Text(f"log file {path} not found yet", style="bold #ffd166"))
                self._log_pos = -1
            return
        if self._log_inode != st.st_ino or st.st_size < self._log_pos:  # rotated
            self._log_inode = st.st_ino
            self._log_pos = max(0, st.st_size - 60_000) if self._log_pos <= 0 else 0
        if st.st_size == self._log_pos:
            return
        with open(path, "rb") as fh:
            fh.seek(self._log_pos)
            chunk = fh.read(400_000)
            self._log_pos = fh.tell()
        for raw in chunk.decode("utf-8", "ignore").splitlines():
            log.write(self.format_log(raw))

    @staticmethod
    def format_log(raw: str) -> Text:
        try:
            d = json.loads(raw)
        except ValueError:
            return Text(raw[:300], style="#41ff7f")
        lvl = d.get("level", "INFO")
        extra = {k: v for k, v in d.items() if k not in ("ts", "level", "logger", "msg", "settings", "exc")}
        t = Text()
        t.append(d.get("ts", "")[11:23] + " ", style="#46b46c")
        t.append(f"{lvl:<7} ", style=LEVEL_STYLE.get(lvl, "#41ff7f"))
        t.append(f"{d.get('logger', '').replace('fre_platform.', ''):<18} ", style="#46b46c")
        t.append(d.get("msg", ""), style=LEVEL_STYLE.get(lvl, "#41ff7f") if lvl != "INFO" else "#7dff9b")
        if extra:
            t.append("  " + json.dumps(extra, default=str)[:220], style="#41ff7f")
        if d.get("exc"):
            t.append("\n" + d["exc"][-800:], style="red")
        return t

    # ----------------------------------------------------------- actions
    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def _request(self, action: str, label: str) -> None:
        try:
            cid = control.submit(self.data, action)
        except OSError as exc:
            self.notify(f"cannot submit request: {exc}", severity="error")
            return
        self.notify(f"{label} requested ...", title=label)
        self._await_result(cid, label)

    @work(thread=True, exclusive=False)
    def _await_result(self, cid: str, label: str) -> None:
        first = control.wait(self.data, cid, timeout=10, final_only=False)
        if first is None:
            self.call_from_thread(self.notify, "the platform did not pick up the request - is it running?",
                                  title=label, severity="error", timeout=8)
            return
        res = control.wait(self.data, cid, timeout=900) if first.get("status") in ("accepted", "running") else first
        res = res or first
        sev = "error" if res.get("status") == "error" else "information"
        self.call_from_thread(self.notify, res.get("message", res.get("status", "?")), title=label,
                              severity=sev, timeout=10)

    def action_backup(self) -> None:
        self._request("backup", "Backup")

    def action_ir_probe(self) -> None:
        self._request("ir_probe", "IR probe")

    def action_flush(self) -> None:
        self._request("flush", "Upload")

    def action_restart(self) -> None:
        if self.mode != "service":
            self.notify("running in the foreground (no service) - quit and start again instead", severity="warning")
            return

        def done(yes: bool | None) -> None:
            if yes:
                self._restart_service()
        self.push_screen(ConfirmScreen("Restart the fre-platform service?\n(collection pauses ~20 s)", danger=True),
                         done)

    @work(thread=True)
    def _restart_service(self) -> None:
        r = subprocess.run(["sudo", "-n", "systemctl", "restart", "fre-platform"], capture_output=True, text=True,
                           timeout=120)
        if r.returncode == 0:
            self.call_from_thread(self.notify, "service restarted", title="Service")
        else:
            self.call_from_thread(self.notify, (r.stderr or r.stdout or "failed").strip()[:200], title="Service",
                                  severity="error")

    def action_quit(self) -> None:
        if self.mode == "foreground":
            def done(yes: bool | None) -> None:
                if yes:
                    self.exit()
            self.push_screen(ConfirmScreen("Quit and STOP collection?\n(no service is running)", danger=True), done)
        else:
            self.exit()  # service keeps collecting


def run_tui(cfg: Settings, mode: str = "service", platform=None) -> int:
    FreApp(cfg, mode=mode, platform=platform).run()
    return 0
