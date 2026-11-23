> **Integrated (23 Nov 2026):** the relay logger now runs inside the `fre-platform` service
> (src/fre_platform/relay/). Service name: `fre-platform` (was `fre-relay-logger`); live view:
> `python3 main.py` (panel 2); MongoDB: `fre_dataset_v2.relay_events` (uploaded automatically).
> Data files and their format are unchanged (~/FRE-Relay-Data). Firmware: firmware/esp32-relay-monitor/.

# FRE Relay Monitor

Records **when the radio ripple-control receiver (FRE) actually switches its
relays**, with UTC timestamps, so relay actions can be matched against the
telegrams decoded by the SDR platform
([`FRE-Detection-Mitigation`](../FRE-Detection-Mitigation), Tarik).

Part of Ripple-Control-Paper (RRCR, CYSECDIGITAL-Lab, THM). This is the
**ground-truth channel**: the SDR tells us what was broadcast, this tells us what
the receiver did about it. Receive-only, nothing is ever sent to the FRE.

Owner: Rojda Özevli

```
FRE (Syna EK 393)  K1..K4 relay contacts
   │  dry contacts, 3.3 V logic only
   ▼
ESP32-S3  (esp32/click_logger_multi)   detects every relay change, heartbeat every 60 s
   │  USB serial, 115200 baud
   ▼
Raspberry Pi  (logger/relay_logger.py, systemd service)
   │  stamps each line with the Pi's NTP-synced UTC clock
   ▼
~/FRE-Relay-Data/relays_YYYY-MM-DD.jsonl + .csv  ──(optional, later)──►  MongoDB Atlas
                                                                fre_dataset.relay_events
```

The SDR platform and this logger run on the **same Pi**, so both datasets use
the same clock and can be joined on `ts_utc`.

## The receiver and its relays

Device: Langmatz LK2-E5-0455, 129.1 kHz (DCF49), sold as Syna EK 393.
From the Syna sheet *"Installations- und Funktionsbeschreibung
Funkrundsteuerempfänger"* (Stand 18. Mai 2012, p. 2):

| Relay | Meaning | Active position | Screws (a / c / b) |
|---|---|---|---|
| K1 | 100 % feed-in allowed | `a` (or `b` if K2–K4 also `b`) | 3 / 4 / 5 |
| K2 | max. 60 % | `a` | 6 / 7 / 8 |
| K3 | max. 30 % | `a` | 9 / 10 / 11 |
| K4 | 0 % (no feed-in) | `a` | 12 / 13 / 14 |
| K5, K6 | not used | – | 15–20 |

- Contacts are potential-free changeover contacts (250 V / 25 A rated).
- The sheet says only one relay is energized at a time. During switchover
  (up to 100 ms) both or no contacts may be closed for a moment.

**What position `a` vs `b` means** (Syna manual + several grid-operator FRE
docs + our own observation of the unit switching):

- **`b` = OFF / rest / de-energized.** Confirmed directly: when the unit
  arrived unpowered, all four relays sat at `b`.
- **`a` = ON / active / "this level is being requested".** Once powered and
  synced, the receiver drives relays to `a`.

Read the power level from **all four relays together** (like the speed buttons
on a fan - only one is "on" at a time):

| Relay at `a` | Power level |
|---|---|
| K1 | 100% |
| K2 | 60% |
| K3 | 30% |
| K4 | 0% |
| (all four at `b`) | 100% (fallback) |

So one relay alone doesn't tell you the level: e.g. **K1 going `a`->`b`** means
"100% handed off" - look at which other relay went to `a` at the same moment to
know the new level (that other relay's % ), or if all stay `b` it's still 100%.

Two things still to confirm on *our* unit (task #12), because it idled with all
four at `a` which does not match the "one relay at a time" rule: when a real
reduction command lands, verify (a) the relay pattern and (b) that the inverter's
feed-in actually changes. Sources:
[die-energie.de prüfanleitung](https://die-energie.de/fileadmin/netz/Einspeiser/Weitere-Informationen/Schaltplan_und_pr%C3%BCfanleitung_zum_Rundsteuerempf%C3%A4nger_2024_2.pdf),
[Form A/B/C contacts](https://forum.digikey.com/t/form-a-b-c-kontaktkonfiguration-in-relais-reedschalter/10714).
- Screws **1 and 2 are L and N (230 V mains). Never touch them.**
- Status LED: synchronised = on 10 s / off 1 s; no sync = on 1 s / off 1 s (p. 3).

## Wiring

| FRE screw | Connect to |
|---|---|
| 4, 7, 10, 13 (all `c`) | GND rail (ESP32 GND) |
| 3 (K1 `a`) | ESP32 GPIO 4 |
| 6 (K2 `a`) | ESP32 GPIO 5 |
| 9 (K3 `a`) | ESP32 GPIO 6 |
| 12 (K4 `a`) | ESP32 GPIO 7 |
| 5, 8, 11, 14 (`b`) | not connected |

No resistors needed: the ESP32's internal pull-ups are used.
Reading: **LOW = contact at `a`**, **HIGH = at `b`** (what each position means
on our unit: see "Observed on our unit" above).
A logic analyzer can be tapped on the same GPIO lines for detailed timing.

> **Safety:** the signal screws sit right next to the 230 V screws. For permanent
> use, use ferrules or solid-core wire (not bare jumper ends), route the wires
> through the cable entry, and keep the cover on.

## Setup on the Raspberry Pi

1. Flash `esp32/click_logger_multi/click_logger_multi.ino` to the ESP32-S3
   (Arduino IDE, board "ESP32S3 Dev Module"). The serial monitor at 115200 baud
   should show `BOOT,...,v2`, then a `STATE,...` line every minute.
2. Plug the ESP32 into the Pi and find its stable port name:
   ```
   ls /dev/serial/by-id/
   ```
3. Install and start the service (creates the data folder and the Python
   environment, installs requirements, sets up systemd):
   ```
   cd ~/Desktop/FRE-Relay-Monitor
   bash deploy/install.sh /dev/serial/by-id/<the-esp-port>
   ```
4. Check:
   ```
   systemctl status fre-relay-logger      # should be "active (running)"
   journalctl -u fre-relay-logger -f      # live view of events
   timedatectl                            # "System clock synchronized: yes"
   ```

Day-to-day commands:

| Task | Command |
|---|---|
| Stop / start | `sudo systemctl stop fre-relay-logger` / `start` |
| Today's events | `grep event ~/FRE-Relay-Data/relays_$(date -u +%F).csv` |
| Is it still recording? | `tail -3 ~/FRE-Relay-Data/relays_$(date -u +%F).csv` |
| After editing the code | `sudo systemctl restart fre-relay-logger` |
| ESP port changed | run `deploy/install.sh` again with the new port |

### Where things live, and updating the code

The data and the Python environment live **outside** the code folder:

| What | Where |
|---|---|
| Code (this folder) | `~/Desktop/FRE-Relay-Monitor/` |
| Data | `~/FRE-Relay-Data/` |
| Python environment | `~/.venvs/fre-relay-monitor/` |

So the code folder can be replaced, re-copied or deleted without losing data.
To update: put the new code folder in place (copy, scp or unzip, any way), then
run `bash deploy/install.sh <port>` again. It only updates and restarts; data
and environment are kept. Never delete `~/FRE-Relay-Data/`.

Should a data file get deleted while the logger runs anyway, it notices within
5 s, starts a new file and writes a `file_reopened` record.

To run by hand instead (e.g. for testing), stop the service first:
`~/.venvs/fre-relay-monitor/bin/python logger/relay_logger.py --port /dev/serial/by-id/<port>`

## Data format

One file per UTC day, in two formats with the same content:

- `~/FRE-Relay-Data/relays_YYYY-MM-DD.jsonl`: one JSON document per line (for MongoDB)
- `~/FRE-Relay-Data/relays_YYYY-MM-DD.csv`: same records as a table (for Excel/pandas)

| Field | Meaning |
|---|---|
| `ts_utc` | Pi's UTC time when the line arrived (ms precision). Use this for joins. |
| `host` | Pi hostname |
| `schema_version` | record layout version (currently 1) |
| `clock_synced` | was the Pi's clock NTP-synced at that moment (`true`/`false`/empty) |
| `type` | see below |
| `esp_ms` | the ESP32's own stopwatch: milliseconds since the ESP booted (resets to 0 on reboot). **Not** wall-clock time - use `ts_utc` for that. Its value jumps around because each switch happens at a different moment; its *difference* between two events gives the switchover time to 1 ms (unaffected by USB delay). E.g. `90060` then `90123` = 63 ms apart. |
| `fw` | ESP firmware version (on `boot` records) |
| `relay`, `position`, `level_pct` | for `event`: e.g. `K3`, `a`, `30`. `level_pct` is the relay's nominal step from the Syna sheet, a label only, not a claim about the plant's actual output (see "Observed on our unit") |
| `K1`..`K4` | for `state`: position of every relay (`a`/`b`) |
| `detail` | text for logger records (port, error message, gap length) |
| `raw` | the exact line the ESP sent |

Record types:

| `type` | Written when |
|---|---|
| `event` | a relay changed position (after 20 ms debounce) |
| `state` | heartbeat every 60 s with all four positions (proves the monitor was alive) |
| `boot` | ESP32 (re)started |
| `logger_start` | logger process started |
| `serial_connected` / `serial_lost` / `serial_unavailable` | USB connection to the ESP came / went / can't be opened |
| `gap` / `data_resumed` | no line from the ESP for > 180 s / lines arrive again |
| `disk_low` | less than 500 MB free |
| `file_reopened` | the data file was deleted or moved while open; a new one was started |
| `other` | unreadable line (boot noise), kept in `raw` |

**Reading switchover timing:** a real command shows up as two `event` records,
for example `K1 → b` then `K3 → a`. Use the `esp_ms` difference (1 ms
resolution, unaffected by USB delay) for the switchover duration. Very short
states (< 20 ms) are hidden by the debounce.

**Live console view:** `journalctl -u fre-relay-logger -f` prints events in a
readable form (the saved files keep the raw machine format):

```
13:05:48.447 UTC  K1 (100%)  ->  b (rest/off)
13:05:48.697 UTC  K2 (60%)   ->  a (active/on)   (+63 ms since last)
```

It shows the UTC time, the relay with its power level, the position with its
meaning, and - when two switches land close together (a switchover) - the gap
between them.

**Storage:** about 0.5 MB/day for both formats together (dominated by the
heartbeat), about 175 MB/year. Not a concern on the Pi's SD card.

## MongoDB (optional, not yet enabled)

`logger/upload_to_mongo.py` copies the JSONL files into Atlas. It was checked
against the SDR platform's code (`tarikbilla/FRE-Detection-Mitigation`, commit
`ab2d7fd`, 14 Nov 2026) and tested against a mock database, **but not yet
against the real Atlas**. Proposal for Tarik to confirm:

- **Same settings as the platform**, read the same way its `main.py` does:
  `$FRE_ENV_FILE`, else `/etc/fre-platform/fre.env`, else
  `~/Desktop/FRE-Detection-Mitigation/.env`; environment variables win.
  Credentials as `FRE_MONGODB_URI` or `FRE_MONGODB_USERNAME` + `_PASSWORD` +
  `_HOST` (builds the identical URI). Database from `FRE_MONGODB_DB`. No extra
  credentials needed. The platform's installer makes `fre.env` readable by
  its service user, so no `sudo` is needed when both run as the same user.
- **Same document conventions**: every document carries `schema_version`,
  `dataset_version`, `experiment_id`, `station_id`, `ts` (UTC date) and
  `source` (`relay-monitor`), using the platform's defaults when unset. Relay
  events therefore join with the `telegrams` collection on `ts` / `station_id`.
- **Own collection only**: writes to **`relay_events`**; the platform's four
  collections are never touched.
- **Same idempotent writes** (upsert by deterministic `_id` with
  `$setOnInsert`): re-running never duplicates.
- **Quota**: only one heartbeat per hour is uploaded (all events, boots and
  logger records always), about 4 MB per year. The platform's free-tier guard
  measures the whole database, so this counts toward its 85 % soft limit too.

Questions for Tarik before the first `--commit`:
1. Is `relay_events` in `fre_dataset` OK, or should it go elsewhere?
2. Should `relay_events` get a JSON-schema validator and indexes like the
   platform's collections (e.g. `station_id` + `ts`)?
3. Should relay events use a separate `experiment_id`?

```
cd ~/Desktop/FRE-Relay-Monitor
~/.venvs/fre-relay-monitor/bin/python logger/upload_to_mongo.py              # dry run: settings + what would go up
~/.venvs/fre-relay-monitor/bin/python logger/upload_to_mongo.py --ping       # test the Atlas login, writes nothing
~/.venvs/fre-relay-monitor/bin/python logger/upload_to_mongo.py --since 2026-11-18 --commit
```

The local files stay the source of truth. If uploads should run automatically
later, add a systemd timer or cron entry for the command above.

## Robustness

| Situation | What happens |
|---|---|
| ESP unplugged / USB glitch | `serial_lost` record; reconnects every 5 s; `serial_connected` when back |
| ESP resets | `boot` record; timestamps unaffected (Pi clock is used) |
| ESP connected but silent | `gap` record after 180 s |
| Logger crashes / Pi reboots | systemd restarts it (`Restart=always`, enabled at boot) |
| Power cut | every line is flushed immediately; at most a half line is lost |
| Data file / data folder deleted while running | noticed within 5 s; new file started, `file_reopened` record |
| Pi clock not synced yet after boot | `clock_synced=false` on those records, so they can be filtered |
| Write error / disk nearly full | retries; `disk_low` record below 500 MB |
| Code folder replaced or deleted | data and Python environment are outside it, nothing lost; re-run `install.sh` |
| Code changes later | bump `FW_VERSION` (ESP) or `SCHEMA_VERSION` (logger); old and new data stay distinguishable |

## Files

```
esp32/click_logger_multi/click_logger_multi.ino   ESP32-S3 firmware
logger/relay_logger.py                            Pi logger (serial -> daily JSONL/CSV)
logger/upload_to_mongo.py                         optional Atlas upload (dry run by default)
deploy/install.sh                                 data folder + venv + systemd setup
deploy/fre-relay-logger.service                   systemd unit template
requirements.txt                                  pyserial, pymongo
```

## Open points

- Rest state of our unit is all four relays at `a` (latches checked by eye,
  logger agrees), which does not match the 2012 Syna sheet. Confirm with Syna /
  the operator what each position means for this unit, and check it against
  the first real commands in the log.
- 18 Nov 2026 (setup day): the first test data (manual latch tests and
  heartbeats, about 14:35 to 15:50 UTC) was lost when the code folder, which
  then also held the data, was deleted. It contained no real broadcast
  commands. Since then data lives outside the code folder (`~/FRE-Relay-Data`).
  The dataset starts with the reinstall on 18 Nov (firmware v2).
- MongoDB collection and naming still to be agreed with Tarik.
