# FRE Detection & Mitigation Research Platform

Python platform that **receives** long-wave radio ripple-control (FRE / EFR) signals with an
**Airspy HF+ Discovery** on a Raspberry Pi, extracts features, detects anomalies, and stores a
versioned research dataset in **MongoDB Atlas** (database `fre_dataset`).

> **Receive-only.** The platform contains no transmit code path and the Airspy HF+ Discovery
> has no transmitter. Attack scenarios (jamming, spoofing, flooding...) exist only as
> **software simulations** for defensive research. Do not use it to interfere with utility
> infrastructure.

```
Airspy HF+ Discovery ─► airspyhf_rx (IQ, 192 kS/s) ─► features + detection ─► SQLite buffer ─► MongoDB Atlas
                                                         (rules + ML)        (survives outages)   (fre_dataset)
```

---

## 1. Quick start (Raspberry Pi)

The Pi used for development already has everything installed. On a fresh Pi:

```bash
# 1. get the code
git clone git@github.com:tarikbilla/FRE-Detection-Mitigation.git ~/Desktop/FRE-Detection-Mitigation
cd ~/Desktop/FRE-Detection-Mitigation

# 2. install dependencies, Python env, config file, ML model and the systemd service
sudo deploy/install.sh --apt --train

# 3. add your MongoDB Atlas connection string (see section 3)
sudo nano /etc/fre-platform/fre.env

# 4. restart and check
sudo systemctl restart fre-collector
sudo systemctl status fre-collector
```

From then on the collector **starts automatically at boot** and restarts itself if it crashes.

Requirements: Raspberry Pi with Kali / Raspberry Pi OS / Debian, Python ≥ 3.11, the Airspy
HF+ Discovery connected by USB, `airspyhf_rx` / `airspyhf_info` (from
[airspyhf](https://github.com/airspy/airspyhf)), an **LF-capable antenna** on the receiver's
antenna input, and internet access for Atlas.

---

## 2. How to run it

All commands are run from the repository folder. `fre-platform` is installed into `.venv`:

```bash
cd ~/Desktop/FRE-Detection-Mitigation
source .venv/bin/activate          # or prefix commands with .venv/bin/
export FRE_ENV_FILE=/etc/fre-platform/fre.env   # use the service's settings
```

### Live view of the received FRE data

The collector runs **forever** (until Ctrl+C / `systemctl stop`) and prints every received
block when started with `--live`:

```bash
# A) watch the background service live (the service keeps running and uploading)
FRE_ENV_FILE=/etc/fre-platform/fre.env .venv/bin/fre-platform watch

# B) or run the collector yourself in the foreground with live output
sudo systemctl stop fre-collector
FRE_ENV_FILE=/etc/fre-platform/fre.env .venv/bin/fre-platform --log-level WARNING collect --live
```

Example output (one block per second, one line per channel):

```
01:23:45.120  #1234   band floor  -136.5 dBFS/Hz  power  -83.2 dBFS  buffer 85  atlas online (5120 up)
    DCF77     77.5 kHz  P  -62.4 dBFS  SNR   38.2 dB  off   +0.3 Hz  FSK 0.00  lines 0  normal     present
    DCF49    129.1 kHz  P  -70.1 dBFS  SNR   31.5 dB  off   -1.8 Hz  FSK 0.21  lines 0  normal     present
    HGA22    135.6 kHz  P  -71.0 dBFS  SNR   30.2 dB  off   +2.4 Hz  FSK 0.00  lines 0  normal     present
    DCF39    139.0 kHz  P  -69.8 dBFS  SNR   33.0 dB  off  +41.0 Hz  FSK 0.18  lines 0  suspicious present FREQ_OFFSET
```

`P` = channel power, `SNR` = signal-to-noise ratio, `off` = carrier frequency offset,
`FSK` = fraction of the block with FSK telegram activity, `lines` = unexpected spectral lines,
then the verdict (`normal` / `suspicious` / `anomalous`), the signal state and any rule codes.

### Other commands

| What you want | Command |
|---|---|
| Check the effective settings (password hidden) | `fre-platform show-config` |
| Test the MongoDB Atlas connection | `fre-platform db-check` |
| Collect real data for 30 s in the foreground (no upload) | `sudo systemctl stop fre-collector` then `fre-platform --log-format text collect --max-blocks 30 --no-upload` |
| Collect continuously in the foreground | `fre-platform --log-format text collect` (Ctrl+C to stop) |
| See what is waiting in the local buffer | `fre-platform buffer-status` |
| Upload everything buffered right now | `fre-platform flush` |
| Run a simulated attack scenario through the pipeline | `fre-platform simulate --scenario mixed --blocks 60 --no-upload` |
| (Re)train the ML models | `fre-platform train --blocks-per-scenario 30 --register` |
| Compare mitigation strategies (simulation) | `fre-platform mitigation-eval --blocks 200` |
| Export a collection to CSV | `fre-platform export --collection measurements --out measurements.csv` |

> Only one program can open the Airspy at a time. Stop the service
> (`sudo systemctl stop fre-collector`) before running `collect` by hand, and start it again
> afterwards.

### Running as a service (normal operation)

```bash
sudo systemctl start   fre-collector      # start
sudo systemctl stop    fre-collector      # stop
sudo systemctl restart fre-collector      # after changing /etc/fre-platform/fre.env
systemctl status fre-collector            # state
journalctl -u fre-collector -f            # live logs (JSON lines)
sudo systemctl disable fre-collector      # do not start at boot
```

Logs are also written to `/var/log/fre-platform/fre-platform.log` (rotated, 5 × 10 MB). A
heartbeat with block counts, verdicts, buffer size and upload state is logged every 60 s and
written to `/var/lib/fre-platform/status.json`.

---

## 3. Configuration

All settings are environment variables with the prefix `FRE_`. The service reads
`/etc/fre-platform/fre.env` (owner `root:kali`, mode `0640`). For development you can use a
`.env` file in the repo folder. **Both are git-ignored; never commit them.**
`.env.example` is the template.

### MongoDB Atlas

1. In Atlas → *Database Access*, create a **new** database user (the old credentials were
   leaked and must be considered compromised – delete/rotate that user).
   Give it **readWrite on `fre_dataset`** only.
2. In Atlas → *Network Access*, allow the Pi's public IP.
3. Put the connection string in the env file:

```bash
sudo nano /etc/fre-platform/fre.env
```
```ini
FRE_MONGODB_URI=mongodb+srv://<user>:<password>@<cluster-host>/?retryWrites=true&w=majority
FRE_MONGODB_DB=fre_dataset
```
4. `FRE_ENV_FILE=/etc/fre-platform/fre.env .venv/bin/fre-platform db-check` must print `"ok": true`,
   then `sudo systemctl restart fre-collector`.

If the URI is not set, the collector keeps running and stores everything in the local buffer;
it is uploaded automatically once the URI is configured.

### Atlas free tier (M0, 512 MB)

The defaults are sized for the free tier. Measured document sizes: measurement ≈ 1.2 KB,
raw spectrum ≈ 2–4 KB. With 4 channels:

| Setting | Data per day | 512 MB lasts |
|---|---|---|
| defaults (measurements every 60 s, raw every 10 min) | ≈ 10 MB incl. indexes | ≈ 50 days |
| measurements every 5 s, raw every 10 s (high resolution) | ≈ 120 MB | ≈ 4 days |

Detections (alerts) are always stored in full. Every 10 minutes the uploader checks the
database size (`dbStats`, data + indexes). Above `FRE_ATLAS_SOFT_LIMIT_PCT` (85 %) of
`FRE_ATLAS_QUOTA_MB` it uploads **only detections and metadata** and keeps measurements/raw
spectra in the local buffer; `watch` then shows `QUOTA: alerts only`. To free space, export
old data (`fre-platform export ...`) and delete it in Atlas, or bump `FRE_DATASET_VERSION`
and drop old versions. The free tier is also throughput-limited, so keep `FRE_BATCH_SIZE`
around 100.

### Main settings

| Variable | Default | Meaning |
|---|---|---|
| `FRE_MONGODB_URI` | – | Atlas connection string (secret) |
| `FRE_MONGODB_DB` | `fre_dataset` | database name |
| `FRE_SOURCE` | `airspyhf` | `airspyhf`, `simulated` or `replay` |
| `FRE_RECEIVER_MODEL` | `Airspy HF+ Discovery` | recorded in device metadata |
| `FRE_CENTER_FREQ_HZ` | `108000` | tuning frequency |
| `FRE_SAMPLE_RATE_HZ` | `192000` | Airspy sample rate |
| `FRE_BLOCK_SECONDS` | `1.0` | analysis block length (0.5 – 10) |
| `FRE_CHANNELS` | `DCF77:77500,DCF49:129100,HGA22:135600,DCF39:139000` | monitored channels `NAME:Hz` |
| `FRE_AIRSPY_AGC` / `FRE_AIRSPY_ATTENUATION` / `FRE_AIRSPY_LNA` | `true` / `0` / `false` | receiver gain |
| `FRE_STATION_ID` | hostname | station name in every document |
| `FRE_DATASET_VERSION` | `2026.11-v1` | dataset version tag |
| `FRE_EXPERIMENT_ID`, `FRE_EXPERIMENT_DESCRIPTION`, `FRE_ANTENNA`, `FRE_LOCATION_LABEL` | | experimental conditions |
| `FRE_RAW_EVERY_N_BLOCKS` | `600` | store a raw spectrum every N blocks (always on alerts) |
| `FRE_MEASUREMENT_EVERY_N_BLOCKS` | `60` | store channel measurements every N blocks (always on alerts) |
| `FRE_PSD_BINS` | `128` | bins in the stored raw spectrum |
| `FRE_ATLAS_QUOTA_MB` / `FRE_ATLAS_SOFT_LIMIT_PCT` | `512` / `85` | free-tier storage guard |
| `FRE_BATCH_SIZE` / `FRE_UPLOAD_INTERVAL_S` | `100` / `30` | upload batching |
| `FRE_MAX_BUFFER_ROWS` | `200000` | local buffer cap (oldest raw spectra dropped first) |
| `FRE_SAVE_IQ_EVERY_N` | `0` | also save raw IQ (`.cf32`) every N blocks, 0 = off |
| `FRE_DATA_DIR` / `FRE_LOG_DIR` / `FRE_MODEL_PATH` | `/var/lib/fre-platform`, ... | file locations |
| `FRE_LOG_LEVEL` / `FRE_LOG_FORMAT` | `INFO` / `json` | logging |

---

## 4. Database schema (`fre_dataset`)

Every document has `schema_version`, `dataset_version`, `station_id`, `experiment_id`,
`device_id`, `source`, `ts` (UTC date), `ts_ns` (exact ns timestamp) and `seq`. `_id` values
are deterministic (`<station>|<ts_ns>|<channel>|<kind>`), so re-uploads never create
duplicates. Collections are created with JSON-schema validators and indexes automatically.

| Collection | Content | Key indexes |
|---|---|---|
| `raw_observations` | block quality (clipping, DC, NTP, gaps), compressed band PSD (256 bins), band noise floor / flatness | `station_id+ts`, `dataset_version+ts` |
| `measurements` | per channel: SNR, carrier offset, FSK levels, spectral lines, activity, ...; `detection` summary (verdict, signal_state, rules, anomaly score, predicted class); `label` (ground truth for simulations) | `station_id+channel+ts`, `dataset_version+label`, `detection.verdict+ts` |
| `detections` | non-normal events: rule hits, ML anomaly/classification, recommended mitigations | `ts`, `verdict+ts`, `rule_hits.code` |
| `devices` | receiver metadata (model, serial, firmware), host, software version | `station_id` |
| `experiments` | experimental conditions (antenna, location, gain, channels, receive-only declaration) | `station_id+started_at` |
| `datasets` | dataset version description, feature names, sampling settings, labels | `created_at` |
| `models` | trained model metadata and evaluation reports (`train --register`) | `metadata.trained_at` |
| `healthchecks` | temporary `db-check` documents (TTL 24 h) | TTL |

Verdicts: `normal`, `suspicious` (warning rule or ML anomaly), `anomalous` (critical rule).
`signal_state`: `present`, `lost` (carrier was there and disappeared), `absent` (never received).

Detection rules: `WIDEBAND_JAMMING`, `NARROWBAND_INTERFERENCE`, `MULTIPLE_EMITTERS`,
`FREQ_OFFSET`, `CARRIER_LOSS`, `TELEGRAM_FLOOD`, `POWER_JUMP`, `ADC_CLIPPING`, plus info codes
`SIGNAL_ABSENT`, `DATA_GAP`, `TIMESTAMP_REANCHORED`. Mitigations are advisory, receiver-side
actions only (hold last valid state, quarantine telegrams, cross-check transmitters, rate-limit,
notify operator...).

---

## 5. Reliability

* Every document is first written to `/var/lib/fre-platform/buffer.sqlite3` (WAL, fsync).
  Rows are deleted only after Atlas acknowledged them.
* Network/Atlas outages: uploads retry with exponential backoff (up to 5 min); nothing is lost.
* Duplicates are prevented locally (primary key) and in Atlas (deterministic `_id` + upsert).
* Documents rejected by schema validation go to a `dead_letter` table instead of blocking.
* Receiver unplugged / `airspyhf_rx` crashes: restarted automatically; dropped blocks counted.
* Timestamps: wall-clock anchor + sample count (sample-accurate), re-anchored if samples are lost.
* systemd: `Restart=always`, starts after network and time sync, enabled at boot.

---

## 6. Testing

```bash
cd ~/Desktop/FRE-Detection-Mitigation
.venv/bin/python -m pytest -q                                        # full suite (~3.5 min on a Pi 4)
sudo systemctl stop fre-collector
FRE_TEST_HARDWARE=1 .venv/bin/python -m pytest -q -m hardware        # real Airspy capture
sudo systemctl start fre-collector
FRE_RUN_ATLAS_TESTS=1 FRE_ENV_FILE=/etc/fre-platform/fre.env \
  .venv/bin/python -m pytest -q -m atlas                             # real Atlas (uses fre_dataset_test, dropped afterwards)
```

Tests cover configuration, secret redaction, sources, DSP features, rule & ML detection,
mitigation simulation, the SQLite buffer, MongoDB store (mongomock), offline buffering and
recovery without duplicates, the end-to-end pipeline, and a scan that fails if secrets are
committed.

---

## 7. Maintenance

```bash
# update code and redeploy
cd ~/Desktop/FRE-Detection-Mitigation && git pull && sudo deploy/install.sh

# retrain models (e.g. after collecting labelled field data)
FRE_ENV_FILE=/etc/fre-platform/fre.env .venv/bin/fre-platform train --from-db --register
sudo systemctl restart fre-collector
```

* Buffer growing (`buffer-status` → `rows` high, `uploader.online=false`): check Internet,
  Atlas network access list and `db-check`.
* `dead_letter` > 0: documents failed validation; inspect with
  `sqlite3 /var/lib/fre-platform/buffer.sqlite3 'select collection,id,error from dead_letter limit 5'`.
* Bump `FRE_DATASET_VERSION` when experiment setup or processing changes.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| All channels `signal_state: absent`, SNR < 0 | No antenna signal: connect an antenna suitable for ~130 kHz long wave to the Airspy HF+ Discovery antenna input; try `FRE_AIRSPY_LNA=true` |
| `ADC_CLIPPING` | raise `FRE_AIRSPY_ATTENUATION` or turn the LNA off |
| `airspyhf_rx` "device busy" | another program (or a manual `collect`) has the receiver open |
| `db-check` DNS/timeout errors | no Internet, or the Pi's IP is not in the Atlas access list |
| `configuration error` in the journal | fix the value named in the message in `/etc/fre-platform/fre.env` |

## 9. Security

* MongoDB credentials previously committed to this repository were removed from the `main`
  history. Because the repository is public and GitHub can still serve the old commit by its
  SHA, those credentials must be treated as **exposed**. Rotating the database user's password
  is strongly recommended. Until it is rotated, at minimum: restrict Atlas *Network Access* to
  the Pi's IP (no `0.0.0.0/0`), give the user `readWrite` on `fre_dataset` only, and ask GitHub
  Support to purge the cached commit.
* Secrets only live in `/etc/fre-platform/fre.env` (root-owned, 0640) or a local `.env`;
  `.gitignore` blocks `.env`, `*.env` and `*credentials*`; a test fails if a credential-bearing
  URI is ever committed.
* Passwords are redacted from all logs and from `show-config`.
* The service runs as an unprivileged user with systemd sandboxing.

## Project layout

```
src/fre_platform/
  config.py            settings from FRE_* env vars, validation, redaction
  logging_setup.py     JSON logging with secret redaction
  sources/             airspyhf.py (Airspy HF+ Discovery), simulated.py, replay.py
  dsp/features.py      quality checks, PSD, per-channel features
  detection/           rules.py (baseline rules), ml.py (IsolationForest + RandomForest)
  mitigation/          advisor.py (playbook), simulator.py (strategy evaluation)
  storage/             buffer.py (SQLite outbox), mongo.py (Atlas), uploader.py
  pipeline.py          analysis + document building + collection loop
  cli.py               fre-platform command
deploy/                fre-collector.service, install.sh
tests/                 pytest suite
```
