# FRE Detection & Mitigation Research Platform

A receive-only research platform that **observes** long-wave radio ripple-control
(Funkrundsteuerung / EFR / FRE) signals with an **Airspy HF+ Discovery** on a
Raspberry Pi, **decodes the telegrams**, extracts signal features, assesses each
telegram's **authenticity**, and builds a clean, versioned **research dataset** in
**MongoDB Atlas** (database `fre_dataset`) for further analysis and machine learning.

> **Receive-only and defensive.** The platform contains no transmit code path and
> the Airspy HF+ Discovery has no transmitter. It decodes the plaintext broadcast
> time telegram and classifies command telegrams at header level only — it never
> crafts, replays, acts on, or transmits any telegram, and never touches utility
> equipment. Attack conditions (jamming, spoofing, time-replay) exist only as
> in-memory **simulations** for building labelled datasets and defensive research.
> Background and signal spec: the public *BlinkenCity* talk, 38C3 (2024).

```
Airspy HF+ Discovery ─► airspyhf_rx ─► per-channel DDC ─┬─► features ─────────► observations
  (IQ, 192 kS/s)        (receive-only)                  ├─► band spectrum ────► spectra
                                                        └─► FSK demod ─► DIN 19244 decode
                                                                         + authenticity ─► telegrams
                                             SQLite buffer (survives outages) ─► MongoDB Atlas (fre_dataset)
```

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

Detection/ML is a **later phase** (`FRE_ENABLE_DETECTION`, off by default) so the
dataset stays clean; the platform ships the detector/ML code ready for it.

---

## Run it — one command

```bash
cd ~/Desktop/FRE-Detection-Mitigation
python3 main.py
```

That's all. `main.py` switches to the project's virtual environment, loads the settings
file automatically, and starts the whole application: receiver → decoder → database
upload, with a full-screen live dashboard. **Ctrl+C** stops it cleanly (it finishes the
current block and uploads what is buffered).

* If the background service (`fre-collector`) is already running, `main.py` simply
  **shows its live view** instead of starting a second copy (only one program can use
  the Airspy). Closing the view does not stop the service.
* `python3 main.py --foreground` collects in the terminal instead
  (stop the service first: `sudo systemctl stop fre-collector`).
* `python3 main.py --plain` uses scrolling lines instead of the full-screen panel.
* Every other command works through it too, for example
  `python3 main.py db-check`, `python3 main.py buffer-status`,
  `python3 main.py decode 68 0a 0a 68 ...`, `python3 main.py --help`.

In dashboard mode, logs go to `/var/log/fre-platform/fre-platform.log`.

## 1. Installation (Raspberry Pi, once)

```bash
git clone git@github.com:tarikbilla/FRE-Detection-Mitigation.git ~/Desktop/FRE-Detection-Mitigation
cd ~/Desktop/FRE-Detection-Mitigation
sudo deploy/install.sh --apt          # deps, venv, config file, systemd service
sudo nano /etc/fre-platform/fre.env   # add MongoDB Atlas credentials (section 3)
sudo systemctl restart fre-collector
sudo systemctl status fre-collector
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
sudo systemctl stop fre-collector
.venv/bin/fre-platform --log-level WARNING collect --live
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
sudo systemctl {start,stop,restart} fre-collector
systemctl status fre-collector
journalctl -u fre-collector -f           # live JSON logs
```

Logs also go to `/var/log/fre-platform/`; a status snapshot is written to
`/var/lib/fre-platform/status.json` and the latest block to `.../live.json`.

---

## 3. Configuration

Settings are environment variables prefixed `FRE_`, read from
`/etc/fre-platform/fre.env` (root-owned, `0640`) for the service, or a local `.env`
in development. **Both are git-ignored — never commit them.** `.env.example` is the
template.

### MongoDB Atlas

Create a database user with **readWrite on `fre_dataset`** only, allow the Pi's IP
in Atlas *Network Access*, then set either a full URI **or** the three parts:

```ini
FRE_MONGODB_URI=mongodb+srv://<user>:<password>@<cluster-host>/?retryWrites=true&w=majority
# ── or ──
FRE_MONGODB_USERNAME=<user>
FRE_MONGODB_PASSWORD=<password>
FRE_MONGODB_HOST=cluster0.xxxxx.mongodb.net
FRE_MONGODB_DB=fre_dataset
```

`fre-platform db-check` must print `"ok": true`. If the URI is unset, the collector
keeps running and buffers locally until it is configured.

**Free tier (M0, 512 MB).** Defaults target ~10 MB/day (≈ 50 days). Above
`FRE_ATLAS_SOFT_LIMIT_PCT` (85 %) of `FRE_ATLAS_QUOTA_MB` the uploader keeps bulk
data (spectra/observations) local and uploads only telegrams and metadata.

### Key settings

| Variable | Default | Meaning |
|---|---|---|
| `FRE_MONGODB_URI` / `_USERNAME`+`_PASSWORD`+`_HOST` | – | Atlas credentials (secret) |
| `FRE_MONGODB_DB` | `fre_dataset` | database name |
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
| `FRE_LOG_LEVEL` / `FRE_LOG_FORMAT` | `INFO` / `json` | logging |

---

## 4. Dataset schema (`fre_dataset`)

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
sudo systemctl stop fre-collector
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
src/fre_platform/
  config.py            settings, validation, secret redaction
  logging_setup.py     JSON logging with secret redaction
  sources/             airspyhf.py, simulated.py (incl. telegram injection), replay.py
  dsp/                 features.py, demod.py (receive-only FSK 8E1 demodulator)
  protocol/            framing.py (DIN 19244), telegram.py (decode + authenticity), synth.py (test signals)
  detection/, mitigation/   phase-2 rule/ML detection + mitigation research
  storage/             buffer.py (SQLite outbox), mongo.py (Atlas schema), uploader.py
  pipeline.py          acquisition -> analysis -> documents -> collection loop
  cli.py               the fre-platform command
deploy/                fre-collector.service, install.sh
tests/                 pytest suite
```
