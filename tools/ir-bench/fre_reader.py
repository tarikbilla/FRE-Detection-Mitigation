#!/usr/bin/env python3
"""
FRE Reader - read-only IR (optical) readout for the FRE LK2-E5-0455
(Funkrundsteuerempfaenger, EFR type) via an FTDI optical head.

Protocol: IEC 62056-21 (formerly IEC 1107), mode C readout.
  1. 300 baud, 7E1: send "/?!\r\n" (sign-on, all addresses)
  2. Device answers "/XXXZ<ident>\r\n"  (XXX = manufacturer, Z = baud code)
  3. Send ACK "\x06 0 Z 0 \r\n" -> data readout mode (option 0 = readout only)
  4. Switch to baud Z, receive STX ... ! \r\n ETX BCC

Only read requests are sent. No programming mode, no passwords, no writes.

Usage:
  .venv/bin/python fre_reader.py                    # auto: passive listen + IEC sign-on
  .venv/bin/python fre_reader.py --port /dev/cu.usbserial-XXXX
  .venv/bin/python fre_reader.py --listen-only 30   # just sniff for 30 s at several bauds
"""
import argparse
import datetime as dt
import glob
import pathlib
import sys
import time

import serial

BAUD_CODES = {"0": 300, "1": 600, "2": 1200, "3": 2400, "4": 4800, "5": 9600, "6": 19200}
LOG_DIR = pathlib.Path(__file__).resolve().parent / "logs"


def find_port():
    ports = sorted(glob.glob("/dev/cu.usbserial-*")) + sorted(glob.glob("/dev/ttyUSB*"))
    if not ports:
        sys.exit("No USB serial port found - is the IR head plugged in?")
    return ports[0]


def hexdump(b: bytes) -> str:
    return " ".join(f"{x:02X}" for x in b)


def printable(b: bytes) -> str:
    return b.decode("ascii", errors="replace")


class Log:
    def __init__(self):
        LOG_DIR.mkdir(exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.txt = open(LOG_DIR / f"readout_{ts}.txt", "w")
        self.raw = open(LOG_DIR / f"readout_{ts}.bin", "wb")
        print(f"[log] {LOG_DIR / f'readout_{ts}.txt'}")

    def __call__(self, msg):
        line = f"{dt.datetime.now().isoformat(timespec='milliseconds')}  {msg}"
        print(line)
        self.txt.write(line + "\n")
        self.txt.flush()

    def rx(self, label, data: bytes):
        self.raw.write(data)
        self.raw.flush()
        self(f"{label} RX {len(data)} B")
        if data:
            self(f"    hex : {hexdump(data)}")
            self(f"    text: {printable(data)!r}")


def read_until_quiet(ser, overall=5.0, quiet=0.6):
    """Collect bytes until `quiet` seconds without data or `overall` timeout."""
    buf = bytearray()
    t0 = last = time.monotonic()
    while time.monotonic() - t0 < overall:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            last = time.monotonic()
        elif buf and time.monotonic() - last > quiet:
            break
        time.sleep(0.02)
    return bytes(buf)


def passive_listen(port, log, seconds, bauds):
    log(f"== Passive listen ({seconds}s per setting) ==")
    found = False
    for baud, bs, par in bauds:
        with serial.Serial(port, baud, bytesize=bs, parity=par, stopbits=1, timeout=0) as ser:
            ser.reset_input_buffer()
            data = read_until_quiet(ser, overall=seconds, quiet=seconds)
            log.rx(f"[listen {baud} {bs}{par}1]", data)
            found |= bool(data)
    return found


def iec_readout(port, log, ack_option="0"):
    log("== IEC 62056-21 mode C sign-on ==")
    ser = serial.Serial(port, 300, bytesize=serial.SEVENBITS, parity=serial.PARITY_EVEN,
                        stopbits=1, timeout=0)
    try:
        ser.reset_input_buffer()
        req = b"/?!\r\n"
        ser.write(req)
        ser.flush()
        log(f"TX {req!r}")
        # The IR head echoes our own TX on some heads; read and strip echo
        resp = read_until_quiet(ser, overall=5.0, quiet=2.5)
        if resp.startswith(req):
            log("    (echo of request detected, stripped)")
            resp = resp[len(req):]
        log.rx("[ident]", resp)
        ident = resp.strip(b"\r\n\x00")
        if not ident.startswith(b"/") or len(ident) < 5:
            log("No valid identification received.")
            return False

        ident_s = ident.decode("ascii", "replace")
        manu, zc, dev = ident_s[1:4], ident_s[4], ident_s[5:]
        log(f"Manufacturer: {manu}   baud code: {zc}   device ident: {dev}")
        baud = BAUD_CODES.get(zc, 300)

        ack = b"\x06" + b"0" + zc.encode() + ack_option.encode() + b"\r\n"
        ser.write(ack)
        ser.flush()
        log(f"TX ACK {ack!r} -> switching to {baud} baud")
        time.sleep(0.3)
        ser.baudrate = baud
        ser.reset_input_buffer()

        data = read_until_quiet(ser, overall=60.0, quiet=2.0)
        if data.startswith(ack):
            data = data[len(ack):]
        log.rx("[data]", data)
        parse_block(data, log)
        return True
    finally:
        ser.close()


def parse_block(data: bytes, log):
    if b"\x02" not in data:
        return
    start = data.index(b"\x02") + 1
    end = data.find(b"\x03", start)
    body = data[start:end if end != -1 else len(data)]
    if end != -1 and end + 1 < len(data):
        bcc = 0
        for x in data[start:end + 1]:
            bcc ^= x
        ok = "OK" if bcc == data[end + 1] else f"MISMATCH (calc {bcc:02X})"
        log(f"BCC: {ok}")
    log("== Parsed data lines (OBIS / register) ==")
    for line in body.decode("ascii", "replace").splitlines():
        if line.strip():
            log(f"    {line}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--listen-only", type=float, metavar="SECONDS")
    ap.add_argument("--skip-listen", action="store_true")
    args = ap.parse_args()

    port = args.port or find_port()
    log = Log()
    log(f"Port: {port}")

    sniff = [(9600, 8, "N"), (300, 7, "E"), (2400, 8, "N"), (4800, 7, "E")]
    if args.listen_only:
        passive_listen(port, log, args.listen_only, sniff)
        return
    if not args.skip_listen:
        passive_listen(port, log, 3, sniff)
    if not iec_readout(port, log):
        log("Retrying sign-on once (some devices need a wake-up)...")
        time.sleep(1.5)
        iec_readout(port, log)


if __name__ == "__main__":
    main()
