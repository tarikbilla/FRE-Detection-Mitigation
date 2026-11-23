# IR bench tools: FRE optical-port investigation (23 Nov 2026)

Stand-alone scripts used on a Mac to investigate the optical (IR) port of the FRE
(Langmatz EK 393 / Syna, type plate LK2-E5-0455, "1 LAN01 00 <serial>", 129.1 kHz)
with an Emlog IR read/write head (ART0027, FTDI FT232R). The production version of
this work is `src/fre_platform/ir/` (runs inside the `fre-platform` service).
All scripts are **read-only**: they send only the IEC 62056-21 sign-on / readout request.

| Script | What it does |
|---|---|
| `fre_reader.py` | passive listen + IEC 62056-21 mode C sign-on + readout, logs hex/text |
| `probe.py`, `probe2.py` | sign-on at 300-28800 Bd, 7E1/8N1, three request variants |
| `addr_sweep.py` | sign-on addressed with every number on the type plate, with/without wake-up |
| `rotate_scan.py` | repeats the sign-on while the head is rotated (alignment search) |
| `listen.py` | timestamped passive listen (e.g. while power-cycling the FRE) |
| `monitor.py` | 30 min loop: sign-on + full readout on answer + passive listen |
| `pi/ir_sweep*.py` | earlier sweeps run on the Pi (moved from the Pi home folder) |

Run: `python3 fre_reader.py --port /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<id>-if00-port0`
(stop the `fre-platform` service first or set `FRE_IR_ENABLED=false`: one port, one reader).

## Result (logs in `logs-2026-11-23-mac/`)

The head echoed every request (TX/RX working), but the FRE never answered: 300,
600, 1200, 2400, 4800, 9600, 19200, 28800 Bd; 7E1 and 8N1; broadcast and addressed
sign-on (`<serial>`, `00<serial>`, `LAN0100<serial>`, `1LAN0100<serial>`, `LK2-E5-0455`, ...);
NUL wake-up preamble; power cycle; 30 min / 200 requests. Grid-operator documents
list the EK 393's optical port as IEC 62056-21 / IEC 1107 *parametrisation* interface,
so it most likely needs the vendor tool (or is disabled in the delivered parameter
set). Open question for Langmatz / the grid operator.
