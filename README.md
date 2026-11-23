# FRE Detection & Mitigation Research Platform

A receive-only research platform for long-wave radio ripple control
(Funkrundsteuerung / EFR / FRE). **One application** on a Raspberry Pi records every
data source of the testbed and builds one versioned **research dataset** in
**MongoDB Atlas** (database **`fre_dataset_v2`**):

| Source | Hardware | What it records | Collection |
|---|---|---|---|
| **SDR** | Airspy HF+ Discovery | what was **broadcast**: decoded telegrams + authenticity, signal features, spectra | `telegrams`, `observations`, `spectra`, `sessions` |
| **Relay monitor** | ESP32-S3 on the FRE's K1..K4 dry contacts | what the receiver **did**: every relay switch (ground truth) | `relay_events` |
| **IR probe** | Emlog IR head (FTDI) on the FRE's optical port | read-only IEC 62056-21 probes of the receiver | `ir_readouts` |

Plus a **4-panel live dashboard**, a **daily local backup** of all MongoDB data and
a durable local buffer that survives network/Atlas outages.

> **Receive-only and defensive.** The platform contains no transmit code path and
> the Airspy HF+ Discovery has no transmitter. It decodes the plaintext broadcast
> time telegram and classifies command telegrams at header level only — it never
> crafts, replays, acts on, or transmits any telegram, and never touches utility
> equipment. The relay monitor only *reads* dry contacts; the IR probe only sends
> the IEC 62056-21 sign-on / readout request (never programming mode or writes).
> Attack conditions (jamming, spoofing, time-replay) exist only as in-memory
> **simulations** for building labelled datasets and defensive research.
> Background and signal spec: the public *BlinkenCity* talk, 38C3 (2024).

```
Airspy HF+ ─► airspyhf_rx ─► per-channel DDC ─┬─► features ───────────────► observations
 (192 kS/s)                                   ├─► band spectrum ──────────► spectra
                                              └─► FSK demod ─► DIN 19244
                                                  decode + authenticity ──► telegrams
FRE K1..K4 ─► ESP32-S3 (fw v2) ─► USB serial ─► relay monitor ──────────► relay_events
                                                 └─► ~/FRE-Relay-Data/relays_YYYY-MM-DD.{jsonl,csv}
FRE optical port ─► IR head ─► IEC 62056-21 probe (every 5 min) ────────► ir_readouts

        all ─► SQLite buffer (survives outages) ─► uploader ─► MongoDB Atlas (fre_dataset_v2)
                                       daily 03:30 ─► /var/lib/fre-platform/backups/<day>/
```

One systemd service, **`fre-platform`**, runs everything (it replaced the former
`fre-collector` and `fre-relay-logger` services on 23 Nov 2026). Each part runs in
its own supervised thread: if the Airspy, the ESP32 or the IR head is unplugged,
only that part waits and reconnects; the others keep collecting.

---

## What this dataset is for

The dataset is designed to let researchers study **how to tell authentic FRE
signals from false or modified ones**, so receivers can be protected against an
attacker who injects spoofed/replayed telegrams:

* **`telegrams`** — every decoded telegram with its raw bytes, checksum result,
  decoded content (for the plaintext time broadcast), and a deterministic
  **authenticity assessment** (clock offset vs the receiver's trusted clock,
  received-signal fingerprint, plausibility). The time telegram has *no* integrity
  or replay protection, so a spoofed timestamp stands out as a large clock offset.
* **`observations`** — per-channel signal-feature time-series, ready for ML.
* **`spectra`** — periodic band spectra + capture quality.
* **`sessions`** — full reproducibility metadata for every capture run.
* **`relay_events`** — the receiver's actual reaction (K1..K4 switching), joinable
  with `telegrams` on `ts` / `station_id`: broadcast vs. what the device did.
* **`ir_readouts`** — every read-only probe of the receiver's optical port.

Detection/ML is a **later phase** (`FRE_ENABLE_DETECTION`, off by default) so the
dataset stays clean; the platform ships the detector/ML code ready for it.

---

## Run it — one command

```bash
fre                 # from any terminal on the Pi  (or double-click "FRE Platform" on the desktop)
```

`fre` (= `python3 ~/Desktop/FRE-Detection-Mitigation/main.py`) does everything:

1. checks that no legacy service holds the hardware,
2. **starts the `fre-platform` service if it is not running** and waits for its first
   heartbeat (Airspy, ESP32, IR head, MongoDB, backups — all in one service),
3. opens the **FRE Platform app** (green-on-black terminal UI).

Quitting the app (`q`) leaves collection running in the service.

| Tab | Content |
|---|---|
| `1` **Overview** | the 4 panels: SDR (with 2-min SNR sparklines) + Pi health + connections · ESP32 relays + IR · MongoDB live counts + ingest rate · backups + config. `z` zooms one panel full screen (again = next, `Esc` = all four) |
| `2` **Timeline** | ALL sources merged by UTC timestamp; each relay switch is matched to the most recent command telegram ("+6.1 s after telegram command (DCF49)") |
| `3` **Telegrams** | the last 500 received telegrams: station, kind, authenticity, clock offset, decoded time, EVU, checksum, SNR, raw bytes |
| `4` **Relays** | live K1..K4 state + today's relay events with switchover gaps (ms) |
| `5` **Logs** | the service log, live, coloured by level |
| `6` **Backups** | timestamped backups (scheduled + manual) and the local time archive |

| Key | Action (executed by the service, never by the app itself) |
|---|---|
| `z` / `Esc` | zoom a quadrant of the Overview / back to the 4-panel view |
| `b` | back up all MongoDB data now (own timestamped folder, never overwrites) |
| `p` | probe the FRE optical (IR) port now |
| `u` | upload the local buffer to MongoDB now |
| `R` | restart the service (asks first) |
| `q` | quit the app (collection continues) |

The app talks to the service through a command inbox (`/var/lib/fre-platform/commands/`),
so it never competes with the service for the receiver, the ESP32 or the IR head.

| Other ways to run | |
|---|---|
| `fre --foreground` | no service: run everything inside the app (quitting stops collection) |
| `fre --classic` | read-only 4-panel dashboard instead of the app |
| `fre --plain` | scrolling SDR lines (for logs / non-interactive terminals) |
| `fre service start\|stop\|restart\|status` | control the service |
| `fre logs` | follow the service journal |
| `fre backup` · `fre backups` · `fre ir-probe` · `fre relay-status` · `fre db-check` · `fre show-config` | one-shot commands |
| `fre --help` | everything else |

## 1. Installation (Raspberry Pi, once)

```bash
git clone git@github.com:tarikbilla/FRE-Detection-Mitigation.git ~/Desktop/FRE-Detection-Mitigation
cd ~/Desktop/FRE-Detection-Mitigation
sudo deploy/install.sh --apt          # deps, venv, config file, systemd service
sudo nano /etc/fre-platform/fre.env   # add MongoDB Atlas credentials (section 3)
sudo systemctl restart fre-platform
sudo systemctl status fre-platform
```

The collector then **runs continuously, starts at boot, and restarts after a
crash**. Requirements: Raspberry Pi (Kali / Raspberry Pi OS / Debian), Python ≥ 3.11,
Airspy HF+ Discovery on USB, `airspyhf_rx`/`airspyhf_info`, an **LF antenna** on the
receiver, and internet access for Atlas.

---

## 2. How to run and watch it

Run from the repo folder with the service's settings:

```bash
cd ~/Desktop/FRE-Detection-Mitigation
export FRE_ENV_FILE=/etc/fre-platform/fre.env
```

### Live view (received signals + decoded telegrams)

```bash
# follow the running service without stopping it
.venv/bin/fre-platform watch

# or run in the foreground with live output (Ctrl+C to stop)
sudo systemctl stop fre-platform
.venv/bin/fre-platform --log-level WARNING collect --live   # SDR only
```

```
02:24:38.120  #1234  band floor -136.5 dBFS/Hz  power -83.2 dBFS  buffer 0  atlas online (5120 up, 2 MB used)  telegrams 37 (37 time)
    DCF49    129.1 kHz  P -70.1 dBFS  SNR 31.5 dB  off  -1.8 Hz  FSK 0.21  lines 0  normal     present
        >> telegram [ok ] time    time=2026-11-14T02:24:38+00:00  authentic_candidate (clock -1s)  680a0a68570000...16
```

A spoofed/replayed time telegram shows up as `implausible (clock +3600s)`.

### Other commands

| Goal | Command |
|---|---|
| Effective settings (secrets hidden) | `fre-platform show-config` |
| Test MongoDB Atlas connectivity | `fre-platform db-check` |
| Local buffer / collector status | `fre-platform buffer-status` |
| Upload buffered data now | `fre-platform flush` |
| Simulate authentic telegrams | `fre-platform simulate --scenario normal --blocks 120 --no-upload` |
| Simulate a time-spoof capture | `FRE_* …` + `simulate` with a spoofing source (see tests) |
| Export a collection to CSV/Parquet | `fre-platform export --collection telegrams --out telegrams.csv` |
| (phase 2) train ML models | `fre-platform train --blocks-per-scenario 30` |
| (phase 2) mitigation study | `fre-platform mitigation-eval --blocks 200` |

Only one program can open the Airspy at a time — stop the service before a manual
`collect`, and start it again afterwards.

### Service control

```bash
sudo systemctl {start,stop,restart} fre-platform
systemctl status fre-platform
journalctl -u fre-platform -f            # live JSON logs (all subsystems)
```

Logs also go to `/var/log/fre-platform/`. State files in `/var/lib/fre-platform/`
(read by the dashboard): `live.json` (latest SDR block), `status.json` (collector),
`relay_live.json`, `ir_live.json`, `mongo_stats.json`, `platform.json` (thread health).

---

## Data storage — everything by timestamp

Every record of every source carries its own UTC timestamp `ts` (ms precision; the
Pi is NTP-synced and runs in UTC) and is saved **three times, always ordered by that time**:

| Where | Layout | Purpose |
|---|---|---|
| **MongoDB** `fre_dataset_v2` | `_id` = `<UTC ts>|<station>|<source>|…`, e.g. `2026-11-23T15:24:27.412Z|kali-raspberrypi|DCF49|0` | the `_id` index *is* a time index: `find().sort({_id:1})` is chronological in every collection; ids stay deterministic (no duplicates on re-upload) |
| MongoDB view **`timeline`** | telegrams + relay events + IR probes in one stream | `db.timeline.find({ts:{$gte:ISODate("2026-11-24")}}).sort({ts:1})` — broadcast vs. receiver reaction, no extra storage |
| **Local archive** `/var/lib/fre-platform/archive/YYYY/MM/DD/<collection>.jsonl` | filed by the record's `ts` (not by write time); finished days gzip-compressed; kept forever (`FRE_ARCHIVE_KEEP_DAYS`) | complete local copy, independent of network / Atlas / quota; replay with `mongoimport` or pandas |
| **Backups** `/var/lib/fre-platform/backups/<YYYY-MM-DDTHHMMSSZ>/` | folder = UTC start time | daily 03:30 + any manual backup, nothing is overwritten |
| Relay ground-truth files `~/FRE-Relay-Data/relays_YYYY-MM-DD.{jsonl,csv}` | one file per UTC day | unchanged format (relay monitor) |

## Relay monitor (ESP32-S3, ground truth)

Integrated from *FRE-Relay-Monitor* (Rojda Özevli). Firmware:
`firmware/esp32-relay-monitor/click_logger_multi/` (v2, flash with Arduino IDE,
board "ESP32S3 Dev Module"). Wiring (Syna EK 393 / Langmatz LK2-E5-0455):

| FRE screw | Connect to |
|---|---|
| 4, 7, 10, 13 (all `c`) | ESP32 GND |
| 3 (K1 `a`), 6 (K2 `a`), 9 (K3 `a`), 12 (K4 `a`) | GPIO 4, 5, 6, 7 |
| 5, 8, 11, 14 (`b`) | not connected |
| **1, 2 = L/N 230 V** | **never touch** |

K1 = 100 %, K2 = 60 %, K3 = 30 %, K4 = 0 % feed-in; position `a` = active, `b` = rest.
The daily files `~/FRE-Relay-Data/relays_YYYY-MM-DD.{jsonl,csv}` keep their exact
format (they remain the local source of truth); in addition every record is queued
for `relay_events` (heartbeats once per hour, events always and immediately).
Document `_id`s are identical to the former `upload_to_mongo.py`, so nothing is ever
duplicated. Details of the record types: `docs/relay-monitor.md`.

## IR probe (optical port, IEC 62056-21)

Every `FRE_IR_PROBE_INTERVAL_S` (300 s) the platform sends the IEC 62056-21 sign-on
`/?!` at 300 Bd 7E1 to the FRE's optical port; if the receiver identifies itself, it
requests the data readout (option 0, read-only) and stores the result. **Every**
probe is stored (`responded: false` too): on 23 Nov 2026 the unit did not answer at
any speed/format/address (bench logs in `tools/ir-bench/`), and that behaviour over
time is itself research evidence. Plug the head in on the Pi; it is found as
`/dev/serial/by-id/*FTDI*` (Emlog head, FT232R).

## Daily backups

At `FRE_BACKUP_TIME` (03:30 local) every collection of `fre_dataset_v2` (the only
database the application uses) is written to
`/var/lib/fre-platform/backups/<YYYY-MM-DDTHHMMSSZ>/<db>/<collection>.jsonl.gz` (MongoDB
Extended JSON, one document per line) with a `manifest.json` (counts, SHA-256,
redacted settings). Unchanged collections are hard-linked to the previous day
(no download, no extra disk). `FRE_BACKUP_KEEP_DAYS` (14) days are kept; a missed
backup (Pi off) runs at the next start. Restore a collection:

```bash
zcat /var/lib/fre-platform/backups/2026-11-24T033000Z/fre_dataset_v2/telegrams.jsonl.gz \
  | mongoimport --uri "$URI" -d fre_dataset_v2 -c telegrams
```

---

## 3. Configuration

Settings are environment variables prefixed `FRE_`, read from
`/etc/fre-platform/fre.env` (root-owned, `0640`) for the service, or a local `.env`
in development. **Both are git-ignored — never commit them.** `.env.example` is the
template.

### MongoDB Atlas

Create a database user with **readWrite on `fre_dataset_v2` only** (the application never uses another database), allow the Pi's IP
in Atlas *Network Access*, then set either a full URI **or** the three parts:

```ini
FRE_MONGODB_URI=mongodb+srv://<user>:<password>@<cluster-host>/?retryWrites=true&w=majority
# ── or ──
FRE_MONGODB_USERNAME=<user>
FRE_MONGODB_PASSWORD=<password>
FRE_MONGODB_HOST=cluster0.xxxxx.mongodb.net
FRE_MONGODB_DB=fre_dataset_v2
```

`fre-platform db-check` must print `"ok": true`. If the URI is unset, the collector
keeps running and buffers locally until it is configured.

**Free tier (M0, 512 MB per cluster).** Space used by other databases on the cluster (the frozen `fre_dataset`, ~99 MB) is set as `FRE_ATLAS_RESERVED_MB` and counted by the guard without connecting to them. Defaults target ~10 MB/day. Above
`FRE_ATLAS_SOFT_LIMIT_PCT` (85 %) of `FRE_ATLAS_QUOTA_MB` the uploader keeps bulk
data (spectra/observations) local and uploads only telegrams and metadata.

### Key settings

| Variable | Default | Meaning |
|---|---|---|
| `FRE_MONGODB_URI` / `_USERNAME`+`_PASSWORD`+`_HOST` | – | Atlas credentials (secret) |
| `FRE_MONGODB_DB` | `fre_dataset_v2` | the ONLY database the application connects to; `fre_dataset` is refused |
| `FRE_ATLAS_RESERVED_MB` | `0` (deployed: `99`) | MB of other databases on the cluster, for the quota guard |
| `FRE_SOURCE` | `airspyhf` | `airspyhf`, `simulated` or `replay` |
| `FRE_RECEIVER_MODEL` | `Airspy HF+ Discovery` | recorded in session metadata |
| `FRE_CENTER_FREQ_HZ` / `FRE_SAMPLE_RATE_HZ` | `108000` / `192000` | tuning / sample rate |
| `FRE_BLOCK_SECONDS` | `1.0` | analysis block length (0.5–10) |
| `FRE_CHANNELS` | `DCF77:77500,DCF49:129100,HGA22:135600,DCF39:139000` | monitored channels `NAME:Hz` |
| `FRE_DECODE_TELEGRAMS` | `true` | decode received telegrams |
| `FRE_TELEGRAM_REQUIRE_CHECKSUM` | `true` | keep only checksum-valid frames |
| `FRE_TIME_PLAUSIBILITY_TOLERANCE_S` | `5` | max clock offset for a plausible time telegram |
| `FRE_OBSERVATION_EVERY_N_BLOCKS` | `60` | per-channel observation cadence |
| `FRE_SPECTRUM_EVERY_N_BLOCKS` | `600` | band spectrum cadence |
| `FRE_ENABLE_DETECTION` | `false` | phase-2 rule/ML detection (adds verdicts) |
| `FRE_BATCH_SIZE` / `FRE_UPLOAD_INTERVAL_S` | `100` / `30` | upload batching |
| `FRE_ATLAS_QUOTA_MB` / `_SOFT_LIMIT_PCT` | `512` / `85` | free-tier guard |
| `FRE_STATION_ID`, `FRE_DATASET_VERSION`, `FRE_EXPERIMENT_ID`, `FRE_ANTENNA`, `FRE_LOCATION_LABEL` | | metadata / experimental conditions |
| `FRE_RELAY_ENABLED` / `FRE_RELAY_PORT` | `true` / `auto` | relay monitor; `auto` = `/dev/serial/by-id/*CP210*` |
| `FRE_RELAY_DATA_DIR` | `~/FRE-Relay-Data` | daily relay JSONL/CSV files |
| `FRE_RELAY_STATE_UPLOAD_S` | `3600` | upload one relay heartbeat per hour |
| `FRE_IR_ENABLED` / `FRE_IR_PORT` / `FRE_IR_PROBE_INTERVAL_S` | `true` / `auto` / `300` | IR probe |
| `FRE_BACKUP_ENABLED` / `FRE_BACKUP_TIME` / `FRE_BACKUP_KEEP_DAYS` | `true` / `03:30` / `14` | daily backup |
| `FRE_BACKUP_DIR` | `<data_dir>/backups` | where (only `fre_dataset_v2` is backed up) |
| `FRE_RELAY_EXPERIMENT_ID` / `FRE_IR_EXPERIMENT_ID` | `relay-switching-001` / `ir-optical-001` | experiment ids |
| `FRE_ARCHIVE_ENABLED` / `FRE_ARCHIVE_DIR` / `FRE_ARCHIVE_KEEP_DAYS` | `true` / `<data_dir>/archive` / `0` (forever) | local time archive |
| `FRE_LOG_LEVEL` / `FRE_LOG_FORMAT` | `INFO` / `json` | logging |

---

## 4. Dataset schema (`fre_dataset_v2`)

`fre_dataset` (until 23 Nov 2026) is a frozen legacy database. The application never connects to it:
`FRE_MONGODB_DB=fre_dataset` is rejected at start-up, and backups, statistics and the quota
guard only touch `fre_dataset_v2`.

Four collections, created automatically with JSON-schema validators and indexes.
Every time-series document carries `schema_version`, `dataset_version`,
`experiment_id`, `station_id`, `ts` (UTC), `ts_ns` and `seq`. `_id`s are
deterministic, so re-uploads never duplicate.

### `sessions` — reproducibility metadata (one per run)
`device` (model, serial, firmware, receive-only), `host`, `config` (antenna,
location, frequency, sample rate, channels, gain, sampling cadences, scenario),
`feature_names`, `telegram_signal` spec, `authorization` (receive-only).

### `observations` — per-channel feature time-series (ML-ready)
`channel`, `features` (SNR, carrier offset, FSK mark/space/centre levels, spectral
lines, activity, symbol rate, envelope stats…), `signal_state`
(`present`/`lost`/`absent`), compact `quality`, `label` (ground truth in simulation).
Indexed by `dataset_version+channel+ts`, `signal_state+ts`.

### `spectra` — band spectrum + capture quality
`center_freq_hz`, `sample_rate_hz`, `psd` (compact band PSD), `band` (noise floor,
flatness, power), `quality` (clipping, DC, NTP, gaps).

### `telegrams` — decoded received signals + authenticity ⭐
`channel`, `kind` (`time`/`command`/`unknown`), `protocol`, `control`,
`evu_address`, `checksum_ok`, `length`, `raw_hex`, `payload_hex`,
`time` (decoded date/time for broadcasts), and **`integrity`**:

| `integrity` field | meaning |
|---|---|
| `checksum_ok` | frame integrity (a modified frame usually fails) |
| `clock_offset_s` | decoded time − receiver NTP clock (time telegrams) |
| `time_plausible` | offset within tolerance and clock trusted |
| `signal` | received fingerprint: SNR, power, carrier offset, noise floor |
| `assessment` | `authentic_candidate` / `implausible` / `integrity_fail` / `unverified` |

Query authentic vs suspected-spoofed telegrams directly, e.g.
`db.telegrams.find({"integrity.assessment": "implausible"})`.

**Telegram signal** (public spec): FSK, 170 Hz shift, 200 Bd, 8E1; DIN 19244 /
FT1.2 variable frame `68 L L 68 C A … CS 16`, checksum = sum(C…payload) mod 256.

---

### `relay_events` — receiver relay switching (ground truth)
`type` (`event`/`state`/`boot`/`logger_start`/`serial_*`/`gap`/`data_resumed`/…),
`relay`, `position` (`a`/`b`), `level_pct`, `esp_ms` (ESP stopwatch; differences =
switchover time to 1 ms), `K1`..`K4` (on `state`), `fw`, `clock_synced`, `raw`, `host`,
plus the common fields (`experiment_id` = `relay-switching-001`, `source` = `relay-monitor`).

### `ir_readouts` — optical-port probes
`responded`, `echo` (head heard its own request = head OK), `ident`
(`manufacturer`, `baud`, `ident`), `readout` (`lines`, `bcc_ok`, `raw`), `rx_hex`,
`port`, `duration_s`, `error`.

## 5. Reliability

* Every document is written to `/var/lib/fre-platform/buffer.sqlite3` (WAL, fsync)
  before any network I/O; rows are deleted only after Atlas acknowledges them.
* Outages → exponential backoff; nothing lost; duplicates prevented locally
  (primary key) and in Atlas (deterministic `_id` + upsert).
* Telegram framing buffers across blocks, so a frame split over a block boundary is
  still recovered; the UART demod is edge-triggered (robust to filter group delay).
* Receiver unplug / `airspyhf_rx` crash → auto-restart; dropped blocks counted.
* systemd: `Restart=always`, starts after network + time sync, enabled at boot.

---

## 6. Testing

```bash
.venv/bin/python -m pytest -q                                   # full suite (~6 min on a Pi 4)
sudo systemctl stop fre-platform
FRE_TEST_HARDWARE=1 .venv/bin/python -m pytest -q -m hardware   # real Airspy capture
FRE_RUN_ATLAS_TESTS=1 FRE_ENV_FILE=/etc/fre-platform/fre.env \
  .venv/bin/python -m pytest -q -m atlas                        # real Atlas (uses fre_dataset_test, dropped after)
```

Coverage includes config/secret-redaction, sources, DSP features, the FSK
demodulator and DIN 19244 decoder (verified against the published telegram
examples), authenticity assessment, offline buffering/recovery without duplicates,
the end-to-end pipeline, authentic-vs-spoofed telegram labelling, and a scan that
fails if a credential is ever committed.

---

## 7. Maintenance & troubleshooting

```bash
cd ~/Desktop/FRE-Detection-Mitigation && git pull && sudo deploy/install.sh   # update
```

| Symptom | Fix |
|---|---|
| Channels `signal_state: absent`, SNR < 0, no telegrams | No antenna signal: connect an LF antenna (~130 kHz) to the Airspy; try `FRE_AIRSPY_LNA=true` |
| `db-check` TLS "internal error" / timeout | the Pi's public IP is not in Atlas *Network Access* (it can change on DHCP) |
| Buffer growing, `uploader.online=false` | check internet / Atlas access list / `db-check` |
| `ADC_CLIPPING` | raise `FRE_AIRSPY_ATTENUATION` or disable the LNA |
| `airspyhf_rx` “device busy” | another program (or a manual `collect`) holds the receiver |

Bump `FRE_DATASET_VERSION` when the setup or processing changes.

---

## 8. Security

* MongoDB credentials once committed to this repo were removed from `main` history;
  the repo is public and the old commit may still be served by SHA, so treat them as
  **exposed** — rotating the password is recommended, and the Atlas IP allowlist is
  the main protection meanwhile (never `0.0.0.0/0`).
* Secrets live only in `/etc/fre-platform/fre.env` or a local `.env`; `.gitignore`
  blocks `.env`, `*.env`, `*credentials*`; a test fails if a credential URI is committed.
* Passwords are redacted from all logs and `show-config`. The service runs
  unprivileged under systemd sandboxing.

## Project layout

```
main.py                one-command launcher (dashboard / foreground run / every command)
src/fre_platform/
  service.py           integrated platform: supervised threads, shared buffer + uploader
  tui.py               the FRE Platform app (Textual): tabs, keys, notifications
  control.py           command inbox app -> service (backup / IR probe / upload now)
  dashboard.py         4-panel renderers (rich), also --classic   sysinfo.py  Pi health, connections
  config.py            settings, validation, secret redaction
  logging_setup.py     JSON logging with secret redaction
  sources/             airspyhf.py, simulated.py (incl. telegram injection), replay.py
  dsp/                 features.py, demod.py (receive-only FSK 8E1 demodulator)
  protocol/            framing.py (DIN 19244), telegram.py (decode + authenticity), synth.py
  relay/               monitor.py  ESP32 relay monitor (from FRE-Relay-Monitor)
  ir/                  optical.py  IEC 62056-21 read-only probe (from FRE Reader)
  backup.py            daily MongoDB backup (timestamped folders) + retention
  archive.py           local archive: every record filed by its UTC timestamp
  timeid.py            timestamp-first document ids
  detection/, mitigation/   phase-2 rule/ML detection + mitigation research
  storage/             buffer.py (SQLite outbox), mongo.py (schema), uploader.py
  pipeline.py          SDR acquisition -> analysis -> documents
  cli.py               the fre-platform command
firmware/esp32-relay-monitor/   ESP32-S3 firmware (v2) for the relay monitor
analysis/              EFR telegram decoding / fingerprint research scripts (CSV outputs git-ignored)
tools/ir-bench/        bench scripts + logs of the optical-port investigation (23 Nov 2026)
docs/                  relay monitor notes, hardware photos / datasheets
deploy/                fre-platform.service, fre-platform.desktop, install.sh (also installs `fre`)
tests/                 pytest suite
archive/               superseded code (old relay logger, Mac bench copy) - git-ignored
```
