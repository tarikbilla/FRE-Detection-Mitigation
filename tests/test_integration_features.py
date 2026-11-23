"""Relay monitor, IR probe, backups, stats and dashboard of the integrated platform."""
import dataclasses
import gzip
import hashlib
import json
import time
from datetime import datetime, timezone

import pytest

from fre_platform.config import ConfigError, Settings
from fre_platform.ir.optical import IrProbe, parse_data_block, parse_ident, strip_echo
from fre_platform.relay.monitor import RelayMonitor, parse_esp_line, power_level, relay_doc
from fre_platform.storage.buffer import LocalBuffer


@pytest.fixture
def icfg(cfg, tmp_path):
    return dataclasses.replace(cfg, relay_data_dir=tmp_path / "relay", relay_port="/dev/fake-esp",
                               ir_port="/dev/fake-ir", backup_dir=tmp_path / "backups",
                               mongodb_db="fre_dataset_v2", backup_min_free_mb=0)


# ------------------------------------------------------------------ relay
def test_parse_esp_lines():
    assert parse_esp_line("EVT,90060,K3,a") == {"type": "event", "esp_ms": 90060, "relay": "K3",
                                                "position": "a", "level_pct": 30}
    assert parse_esp_line("STATE,1000,K1=a,K2=b,K3=b,K4=b") == {"type": "state", "esp_ms": 1000,
                                                                "K1": "a", "K2": "b", "K3": "b", "K4": "b"}
    assert parse_esp_line("BOOT,12,v2") == {"type": "boot", "esp_ms": 12, "fw": "v2"}
    assert parse_esp_line("EVT,x,K1,a")["type"] == "other"
    assert parse_esp_line("garbage")["type"] == "other"


def test_power_level_from_all_relays():
    assert power_level({"K1": "b", "K2": "a", "K3": "b", "K4": "b"}) == 60
    assert power_level({"K1": "b", "K2": "b", "K3": "b", "K4": "b"}) == 100
    assert power_level({"K1": "a", "K2": "a", "K3": "a", "K4": "a"}) is None  # this unit's rest state


def test_relay_doc_id_is_timestamp_first_and_deterministic(icfg):
    rec = {"ts_utc": "2026-11-20T10:00:00.123+00:00", "host": "pi", "schema_version": 1, "type": "event",
           "esp_ms": 5, "relay": "K1", "position": "b", "level_pct": 100, "raw": "EVT,5,K1,b"}
    doc = relay_doc(rec, icfg)
    key = "pi|2026-11-20T10:00:00.123+00:00|event|EVT,5,K1,b"
    assert doc["_id"] == "2026-11-20T10:00:00.123Z|pi|relay|event|" + hashlib.sha1(key.encode()).hexdigest()[:10]
    assert relay_doc(dict(rec), icfg)["_id"] == doc["_id"]  # re-upload never duplicates
    assert doc["experiment_id"] == "relay-switching-001" and doc["source"] == "relay-monitor"
    assert doc["ts"] == datetime(2026, 11, 20, 10, 0, 0, 123000, tzinfo=timezone.utc)
    assert "ts_utc" not in doc


class FakeSerial:
    lines: list = []

    def __init__(self, *a, **kw):
        self.port = self.baudrate = self.timeout = None
        self.dtr = self.rts = True

    def open(self):
        pass

    def readline(self):
        if FakeSerial.lines:
            return FakeSerial.lines.pop(0)
        time.sleep(0.05)
        return b""

    def close(self):
        pass


def test_relay_monitor_writes_files_buffer_and_live(icfg, tmp_path, monkeypatch):
    import serial
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    monkeypatch.setattr("fre_platform.relay.monitor.clock_synced", lambda: True)
    FakeSerial.lines = [b"\x00\xffBOOT,10,v2\r\n", b"STATE,20,K1=a,K2=a,K3=a,K4=a\r\n",
                        b"STATE,60020,K1=a,K2=a,K3=a,K4=a\r\n", b"EVT,70000,K1,b\r\n", b"EVT,70063,K3,a\r\n"]
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    mon = RelayMonitor(icfg, buf)
    mon.start()
    deadline = time.time() + 5
    while FakeSerial.lines and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    mon.stop()
    mon.join(5)
    files = sorted((tmp_path / "relay").iterdir())
    assert [f.suffix for f in files] == [".csv", ".jsonl"]
    recs = [json.loads(x) for x in files[1].read_text().splitlines()]
    types = [r["type"] for r in recs]
    assert types[:2] == ["logger_start", "serial_connected"] and types.count("event") == 2
    by_coll = buf.count_by_collection()
    # all non-state records + only ONE of the two heartbeats (hourly downsampling)
    assert by_coll["relay_events"] == len(recs) - 1
    live = json.loads((tmp_path / "data" / "relay_live.json").read_text())
    assert live["positions"] == {"K1": "b", "K2": "a", "K3": "a", "K4": "a"} and live["events_today"] == 2
    assert live["fw"] == "v2" and live["recent"][0]["relay"] == "K3"
    buf.close()


# --------------------------------------------------------------------- IR
def test_ir_parsers():
    assert strip_echo(b"/?!\r\n", b"/?!\r\n/LAN5EK393\r\n") == (True, b"/LAN5EK393\r\n")
    ident = parse_ident(b"/LAN5EK393\r\n")
    assert ident == {"manufacturer": "LAN", "baud_code": "5", "baud": 9600, "ident": "EK393", "line": "/LAN5EK393"}
    assert parse_ident(b"") is None and parse_ident(b"\x00\x00") is None
    body = b"0.0.0(123456)\r\nF.F(00)\r\n!\r\n\x03"
    bcc = 0
    for x in body:
        bcc ^= x
    out = parse_data_block(b"\x02" + body + bytes([bcc]))
    assert out == {"lines": ["0.0.0(123456)", "F.F(00)"], "bcc_ok": True, "complete": True}
    assert parse_data_block(b"\x02" + body + bytes([bcc ^ 1]))["bcc_ok"] is False


def test_ir_probe_stores_every_probe(icfg, tmp_path):
    buf = LocalBuffer(tmp_path / "b.sqlite3")
    answers = iter([{"port": "/dev/fake-ir", "echo": True, "responded": False, "rx_hex": "", "duration_s": 5},
                    {"port": "/dev/fake-ir", "echo": True, "responded": True, "rx_hex": "2f",
                     "ident": parse_ident(b"/LAN5EK393\r\n"),
                     "readout": {"lines": ["0.0.0(123456)"], "bcc_ok": True}}])
    probe = IrProbe(dataclasses.replace(icfg, ir_probe_interval_s=0.1), buf, probe_fn=lambda port: next(answers))
    probe.start()
    deadline = time.time() + 5
    while probe.state["probes"] < 2 and time.time() < deadline:
        time.sleep(0.05)
    probe.stop()
    probe.join(5)
    assert buf.count_by_collection()["ir_readouts"] >= 2
    live = json.loads((tmp_path / "data" / "ir_live.json").read_text())
    assert live["responses"] == 1 and live["last_ident"] == "/LAN5EK393" and live["last_lines"] == ["0.0.0(123456)"]
    docs = [r.doc for r in buf.fetch_batch(10)]
    assert {d["responded"] for d in docs} == {True, False}
    assert all(d["experiment_id"] == "ir-optical-001" for d in docs)
    buf.close()


# ----------------------------------------------------------------- schema
def test_new_collections_have_schema_and_accept_docs(mongomock_store, icfg):
    mongomock_store.ensure_schema()
    names = set(mongomock_store.db.list_collection_names())
    assert {"relay_events", "ir_readouts"} <= names
    rec = {"ts_utc": "2026-11-20T10:00:00.000+00:00", "host": "pi", "schema_version": 1, "type": "state",
           "esp_ms": 1, "K1": "a", "K2": "a", "K3": "a", "K4": "a", "raw": "STATE,1,K1=a,K2=a,K3=a,K4=a"}
    doc = relay_doc(rec, icfg)
    res = mongomock_store.upload("relay_events", [(doc["_id"], "insert", doc)])
    assert res.ok == [doc["_id"]] and mongomock_store.counts()["relay_events"] == 1


# ----------------------------------------------------------------- backup
def _fill(client):
    client["fre_dataset_v2"]["telegrams"].insert_many([{"_id": f"t{i}", "ts": datetime.now(timezone.utc),
                                                        "n": i} for i in range(25)])
    client["fre_dataset_v2"]["relay_events"].insert_one({"_id": "r1", "type": "event"})
    client["fre_dataset"]["telegrams"].insert_many([{"_id": f"old{i}"} for i in range(3)])


def test_backup_dumps_all_databases_and_prunes(icfg):
    mongomock = pytest.importorskip("mongomock")
    from fre_platform.backup import list_backups, run_backup
    client = mongomock.MongoClient(tz_aware=True)
    _fill(client)
    cfg = dataclasses.replace(icfg, backup_keep_days=2)
    old = cfg.backup_path / "2026-11-01"
    old.mkdir(parents=True)
    (old / "manifest.json").write_text("{}")
    when = datetime(2026, 11, 24, 2, 30, 0, tzinfo=timezone.utc)
    m = run_backup(cfg, client=client, day="2026-11-24", now=when)
    # only the application's own database is backed up - the legacy fre_dataset is never touched
    assert m["ok"] and m["documents"] == 26 and set(m["databases"]) == {"fre_dataset_v2"}
    assert m["name"] == "2026-11-24T023000Z" and m["trigger"] == "manual"
    assert "2026-11-01" in m["removed_old"] and not old.exists()
    f = cfg.backup_path / "2026-11-24T023000Z" / "fre_dataset_v2" / "telegrams.jsonl.gz"
    lines = gzip.open(f, "rt").read().splitlines()
    assert len(lines) == 25 and json.loads(lines[0])["_id"] == "t0"
    assert "$date" in lines[0]  # extended JSON keeps BSON types for mongoimport
    assert m["databases"]["fre_dataset_v2"]["telegrams"]["sha256"]
    listed = list_backups(cfg)
    assert listed[0]["day"] == "2026-11-24" and listed[0]["name"] == "2026-11-24T023000Z" and listed[0]["ok"]
    # a second (manual) backup the same day gets its own timestamped folder - nothing is overwritten
    m2 = run_backup(cfg, client=client, day="2026-11-24", now=when.replace(hour=9))
    assert m2["name"] == "2026-11-24T093000Z" and len(list_backups(cfg)) == 2
    assert not list(cfg.backup_path.glob("*.partial"))


def test_backup_scheduler_due_logic(icfg):
    from fre_platform.backup import BackupScheduler
    s = BackupScheduler(dataclasses.replace(icfg, backup_time="03:30"))
    tz = datetime.now().astimezone().tzinfo
    assert not s._due(datetime(2026, 11, 24, 3, 0, tzinfo=tz))
    assert s._due(datetime(2026, 11, 24, 3, 30, tzinfo=tz))
    done = icfg.backup_path / "2026-11-24T023000Z"
    done.mkdir(parents=True)
    (done / "manifest.json").write_text(json.dumps({"day": "2026-11-24", "ok": True}))
    assert not s._due(datetime(2026, 11, 24, 9, 0, tzinfo=tz))  # already done today
    assert s._next_run(datetime(2026, 11, 24, 9, 0, tzinfo=tz)).startswith("2026-11-25T03:30")


# ------------------------------------------------------------ stats/config
def test_stats_publisher_counts_all_databases(icfg):
    mongomock = pytest.importorskip("mongomock")
    from fre_platform.service import StatsPublisher
    from fre_platform.storage.mongo import MongoStore
    client = mongomock.MongoClient(tz_aware=True)
    _fill(client)
    pub = StatsPublisher(icfg, lambda: MongoStore(client=client, db_name="fre_dataset_v2"))
    s = pub.sample()
    assert s["ok"] and s["active_db"] == "fre_dataset_v2"
    assert s["databases"]["fre_dataset_v2"]["collections"] == {"relay_events": 1, "telegrams": 25}
    assert set(s["databases"]) == {"fre_dataset_v2"}   # other databases are never read


def test_new_settings_from_env(tmp_path):
    c = Settings.from_env(env={"FRE_SOURCE": "simulated", "FRE_MONGODB_DB": "fre_dataset_v2",
                               "FRE_RELAY_DATA_DIR": str(tmp_path / "r"), "FRE_BACKUP_TIME": "02:15",
                               "FRE_IR_ENABLED": "false", "FRE_DATA_DIR": str(tmp_path)})
    assert c.relay_data_dir == tmp_path / "r" and c.backup_time == "02:15" and not c.ir_enabled
    assert c.backup_path == tmp_path / "backups" and c.backup_db_list == ["fre_dataset_v2"]
    with pytest.raises(ConfigError):
        Settings.from_env(env={"FRE_SOURCE": "simulated", "FRE_BACKUP_TIME": "25:00"})


def test_application_uses_only_the_v2_database():
    assert Settings.from_env(env={"FRE_SOURCE": "simulated"}).mongodb_db == "fre_dataset_v2"   # default
    with pytest.raises(ConfigError, match="legacy"):
        Settings.from_env(env={"FRE_SOURCE": "simulated", "FRE_MONGODB_DB": "fre_dataset"})


def test_quota_guard_counts_reserved_space_without_reading_other_databases():
    mongomock = pytest.importorskip("mongomock")
    from fre_platform.storage.mongo import MongoStore
    client = mongomock.MongoClient(tz_aware=True)
    client["fre_dataset"]["telegrams"].insert_one({"_id": "legacy"})
    store = MongoStore(client=client, db_name="fre_dataset_v2", reserved_mb=99.0)
    used = store.storage_used_mb()
    assert used is None or used >= 99.0          # mongomock may not implement dbStats
    assert store.db.name == "fre_dataset_v2"


# --------------------------------------------------------------- dashboard
def test_dashboard_renders_all_four_panels(icfg, tmp_path):
    from rich.console import Console
    from fre_platform import dashboard
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (d / "relay_live.json").write_text(json.dumps({
        "connected": True, "fw": "v2", "positions": {"K1": "b", "K2": "a", "K3": "b", "K4": "b"},
        "level_pct": 60, "last_line_ts": now, "recent": [{"ts": now, "type": "event", "relay": "K2",
                                                          "position": "a", "level_pct": 60}]}))
    (d / "ir_live.json").write_text(json.dumps({"head_present": False, "probes": 0}))
    (d / "mongo_stats.json").write_text(json.dumps({
        "ok": True, "updated_at": now, "cluster_used_mb": 100, "quota_mb": 512, "latency_ms": 20,
        "databases": {"fre_dataset_v2": {"collections": {"telegrams": 5}, "documents": 5, "data_mb": 1,
                                         "index_mb": 0.1, "active": True}}}))
    console = Console(record=True, width=220, height=60, color_system=None)
    console.print(dashboard.render(icfg, {}))
    text = console.export_text()
    for title in ("1 · SDR & RASPBERRY PI", "2 · FRE RECEIVER", "3 · MONGODB DATABASE", "4 · BACKUPS & CONFIG"):
        assert title in text
    assert "K2 · 60 %" in text and "requested feed-in 60 %" in text and "fre_dataset_v2" in text
    assert "ESP32" in text and "MONGO" in text          # health chips in the header


def test_health_chips_and_alerts(icfg, tmp_path):
    from fre_platform import dashboard
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (d / "relay_live.json").write_text(json.dumps({"connected": False, "positions": {}}))
    (d / "ir_live.json").write_text(json.dumps({"head_present": True, "probes": 5, "responses": 0}))
    (d / "platform.json").write_text(json.dumps({"updated_at": now, "subsystems": {
        "uploader": {"enabled": True, "online": True}, "backup": {}}}))
    states = {s["name"]: s for s in dashboard.health(icfg)}
    assert states["ESP32"]["level"] == "bad" and states["SDR"]["level"] == "bad"     # no live.json
    assert states["IR"]["level"] == "info"            # silent optical port is not an alarm
    assert states["MONGO"]["level"] == "ok" and "CORE" not in states
    alert = dashboard.alerts(list(states.values())).plain
    assert "ESP32 disconnected" in alert and "IR" not in alert


def test_relay_seed_restores_state_and_counts_whole_day(icfg):
    day = datetime.now(timezone.utc).date().isoformat()
    icfg.relay_data_dir.mkdir(parents=True)
    recs = [{"ts_utc": f"{day}T00:00:00.000+00:00", "type": "boot", "fw": "v2"}]
    recs += [{"ts_utc": f"{day}T01:{i // 60:02d}:{i % 60:02d}.000+00:00", "type": "event", "relay": "K2",
              "position": "ab"[i % 2], "level_pct": 60, "raw": "x" * 400} for i in range(400)]  # > 64 KB
    recs.append({"ts_utc": f"{day}T09:00:00.000+00:00", "type": "state", "K1": "b", "K2": "a", "K3": "b", "K4": "b"})
    (icfg.relay_data_dir / f"relays_{day}.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    mon = RelayMonitor(icfg)
    mon.seed_from_files()
    assert mon.state["events_today"] == 400 and mon.state["fw"] == "v2"
    assert mon.state["positions"] == {"K1": "b", "K2": "a", "K3": "b", "K4": "b"} and mon.state["level_pct"] == 60
