"""Command inbox, platform command handling and the interactive app (Textual)."""
import asyncio
import dataclasses
import json
import time
from datetime import datetime, timezone

import pytest

from fre_platform import control
from fre_platform.service import Platform
from fre_platform.storage.buffer import LocalBuffer


@pytest.fixture
def acfg(cfg, tmp_path):
    return dataclasses.replace(cfg, relay_data_dir=tmp_path / "relay", backup_dir=tmp_path / "backups",
                               log_dir=tmp_path / "logs", relay_enabled=False, ir_enabled=False)


def test_inbox_roundtrip(tmp_path):
    cid = control.submit(tmp_path, "flush")
    reqs = control.pending(tmp_path)
    assert [r["id"] for r in reqs] == [cid] and reqs[0]["action"] == "flush"
    control.respond(tmp_path, reqs[0], "done", message="ok")
    assert control.pending(tmp_path) == []
    assert control.result(tmp_path, cid)["message"] == "ok"
    with pytest.raises(ValueError):
        control.submit(tmp_path, "rm -rf")


class FakeUploader:
    def __init__(self):
        self.triggered = 0

    def trigger(self):
        self.triggered += 1


class FakeIr:
    def __init__(self, head=True):
        self.state = {"head_present": head, "probes": 0}
        self.last_result = None

    def probe_now(self):
        n = self.state["probes"]
        self.last_result = {"responded": False, "echo": True}
        self.state["probes"] += 1
        return n


def _platform(acfg, tmp_path):
    p = Platform(acfg, upload=False, sdr=False)
    p.buffer = LocalBuffer(tmp_path / "b.sqlite3")
    return p


def test_platform_handles_flush_and_ir_commands(acfg, tmp_path):
    p = _platform(acfg, tmp_path)
    p.uploader, p.ir = FakeUploader(), FakeIr()
    c1 = control.submit(acfg.data_dir, "flush")
    c2 = control.submit(acfg.data_dir, "ir_probe")
    p.process_commands()
    assert control.result(acfg.data_dir, c1)["status"] == "done" and p.uploader.triggered == 1
    r = control.wait(acfg.data_dir, c2, timeout=5)
    assert r["status"] == "done" and "echo ok" in r["message"]
    p.ir = FakeIr(head=False)
    c3 = control.submit(acfg.data_dir, "ir_probe")
    p.process_commands()
    assert control.result(acfg.data_dir, c3)["message"] == "IR head not connected"
    p.buffer.close()


def test_platform_backup_command(acfg, tmp_path, monkeypatch):
    import fre_platform.backup as backup
    monkeypatch.setattr(backup, "run_backup", lambda cfg: {"name": "2026-11-23T150000Z", "day": "2026-11-23", "documents": 10, "bytes": 2 ** 20,
                                                           "duration_s": 1.0, "ok": True})
    p = _platform(dataclasses.replace(acfg, mongodb_uri="mongodb://x"), tmp_path)
    p.uploader = FakeUploader()
    cid = control.submit(acfg.data_dir, "backup")
    p.process_commands()
    r = control.wait(acfg.data_dir, cid, timeout=5)
    assert r["status"] == "done" and "10 docs" in r["message"]
    p.buffer.close()


def test_telegram_history_written(acfg, tmp_path):
    p = _platform(acfg, tmp_path)
    summary = {"ts": "2026-11-23T15:00:00.000+00:00", "seq": 7, "channels": [
        {"name": "DCF49", "snr_db": 40.0, "telegrams": [{"kind": "time", "assessment": "authentic_candidate",
                                                          "clock_offset_s": 0.4, "checksum_ok": True,
                                                          "raw_hex": "680a0a68", "evu": "0000", "time": None}]}]}
    acfg.data_dir.mkdir(parents=True, exist_ok=True)
    p._on_block(summary)
    data = json.loads((acfg.data_dir / "telegrams_recent.json").read_text())
    assert data["telegrams"][0]["channel"] == "DCF49" and data["telegrams"][0]["seq"] == 7
    p.buffer.close()


def test_app_tabs_and_actions(acfg, tmp_path):
    textual = pytest.importorskip("textual")  # noqa: F841
    from fre_platform.tui import FreApp
    d = acfg.data_dir
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (d / "relay_live.json").write_text(json.dumps({"connected": True, "fw": "v2", "last_line_ts": now,
                                                   "positions": {"K1": "b", "K2": "a", "K3": "b", "K4": "b"},
                                                   "level_pct": 60, "recent": []}))
    (d / "telegrams_recent.json").write_text(json.dumps({"updated_at": now, "telegrams": [
        {"ts": now, "channel": "DCF49", "kind": "time", "assessment": "implausible", "clock_offset_s": 3600.0,
         "checksum_ok": True, "raw_hex": "68", "snr_db": 30.1}]}))
    acfg.relay_data_dir.mkdir(parents=True)
    day = now[:10]
    (acfg.relay_data_dir / f"relays_{day}.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"ts_utc": now, "type": "event", "relay": "K1", "position": "b", "level_pct": 100, "esp_ms": 100},
        {"ts_utc": now, "type": "event", "relay": "K2", "position": "a", "level_pct": 60, "esp_ms": 163},
        {"ts_utc": now, "type": "state", "K1": "b", "K2": "a", "K3": "b", "K4": "b"}]) + "\n")
    acfg.log_dir.mkdir(parents=True)
    (acfg.log_dir / "fre-platform.log").write_text(json.dumps(
        {"ts": now, "level": "WARNING", "logger": "fre_platform.x", "msg": "hello log", "a": 1}) + "\n")

    async def scenario():
        app = FreApp(acfg, mode="service")
        async with app.run_test(size=(200, 55)) as pilot:
            await pilot.pause()
            for key in ("2", "3", "4", "5", "6", "1"):
                await pilot.press(key)
                await pilot.pause()
            await pilot.press("z")                                       # zoom panel 1 ...
            await pilot.pause()
            assert app.query_one("#overview").has_class("zoomed") and app.query_one("#p-sdr").has_class("zoom")
            await pilot.press("escape")                                  # ... and back to 4 panels
            await pilot.pause()
            assert not app.query_one("#overview").has_class("zoomed")
            assert app.query_one("#tl-table").row_count == 3            # 1 telegram + 2 relay events
            assert app.query_one("#tg-table").row_count == 1
            assert app.query_one("#relay-table").row_count == 2          # state rows are not listed
            assert app.query_one("#backup-table").row_count == 0
            await pilot.press("b")
            await pilot.pause()
            reqs = control.pending(d)
            assert [r["action"] for r in reqs] == ["backup"]
            await pilot.press("q")
    asyncio.run(scenario())


def test_telegram_history_survives_restart(acfg, tmp_path):
    acfg.data_dir.mkdir(parents=True, exist_ok=True)
    (acfg.data_dir / "telegrams_recent.json").write_text(json.dumps({"telegrams": [{"channel": "DCF49", "seq": 1}]}))
    p = Platform(acfg, upload=False, sdr=False)
    p.start()
    try:
        assert list(p.recent_telegrams) == [{"channel": "DCF49", "seq": 1}]
    finally:
        p.shutdown()


def test_timeline_matches_relay_switch_to_command_telegram(acfg):
    from fre_platform.tui import FreApp
    d = acfg.data_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "telegrams_recent.json").write_text(json.dumps({"updated_at": "x", "telegrams": [
        {"ts": "2026-11-23T11:37:50.000+00:00", "channel": "DCF49", "kind": "command", "assessment": "unverified",
         "evu": "0460", "raw_hex": "68"},
        {"ts": "2026-11-23T11:37:55.000+00:00", "channel": "DCF49", "kind": "time", "assessment": "authentic_candidate"}]}))
    acfg.relay_data_dir.mkdir(parents=True)
    (acfg.relay_data_dir / "relays_2026-11-23.jsonl").write_text(json.dumps(
        {"ts_utc": "2026-11-23T11:37:56.138+00:00", "type": "event", "relay": "K3", "position": "b",
         "level_pct": 30, "esp_ms": 1}) + "\n")
    ev = FreApp(acfg).timeline_events()
    assert [e["source"] for e in ev] == ["SDR", "SDR", "RELAY"]           # sorted by timestamp
    assert ev[-1]["corr"].startswith("+6.1 s after telegram command (DCF49")


def test_timestamp_ids_sort_chronologically():
    from datetime import timedelta
    from fre_platform.timeid import make_id, ts_key
    t = datetime(2026, 11, 23, 23, 59, 59, 999999, tzinfo=timezone.utc)
    assert ts_key(t) == "2026-11-23T23:59:59.999Z"
    ids = [make_id(t + timedelta(milliseconds=k), "pi", "x") for k in (5, -3, 1000, 0)]
    assert sorted(ids) == [make_id(t + timedelta(milliseconds=k), "pi", "x") for k in (-3, 0, 5, 1000)]


def test_archive_files_every_record_by_its_timestamp(tmp_path):
    import gzip
    from datetime import date
    from fre_platform.archive import TimeArchive
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    arc = TimeArchive(tmp_path / "archive")
    buf.archive = arc
    t1 = datetime(2026, 11, 22, 23, 59, 59, tzinfo=timezone.utc)
    t2 = datetime(2026, 11, 23, 0, 0, 1, tzinfo=timezone.utc)
    buf.put_many([("telegrams", {"_id": "a", "ts": t1, "v": 1}, "insert"),
                  ("relay_events", {"_id": "b", "ts": t2, "v": 2}, "insert"),
                  ("sessions", {"_id": "s", "started_at": t2}, "upsert")])
    day1 = tmp_path / "archive/2026/11/22/telegrams.jsonl"
    day2 = tmp_path / "archive/2026/11/23"
    assert day1.is_file() and {f.name for f in day2.iterdir()} == {"relay_events.jsonl", "sessions.jsonl"}
    rec = json.loads(day1.read_text())
    assert rec["_collection"] == "telegrams" and rec["ts"]["$date"].startswith("2026-11-22T23:59:59")
    # finished days are compressed, today/yesterday stay plain; keep_days prunes
    arc.housekeeping(today=date(2026, 11, 25))
    assert not day1.exists() and gzip.open(str(day1) + ".gz", "rt").read().count("\n") == 1
    arc2 = TimeArchive(tmp_path / "archive", keep_days=1)
    arc2.housekeeping(today=date(2026, 11, 25))
    assert not (tmp_path / "archive/2026/11/22").exists()
    buf.close()
