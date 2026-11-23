#!/usr/bin/env python3
"""ir_sweep_long.py -- like ir_sweep.py, but listens much longer after each
request and timestamps every chunk it receives, so a late/real reply from
the FRE doesn't get lost among (or confused with) the instant self-echo.

Usage: python3 ir_sweep_long.py
Change PORT below if needed ("/dev/ttyUSB0" on Linux, "COM3" etc. on Windows).
"""
import serial
import time

PORT = "/dev/ttyUSB0"      # change to "COM3" (or similar) on Windows
LISTEN_SECONDS = 8.0       # how long to keep listening after sending
POLL_INTERVAL = 0.1        # how often to check for new bytes

BAUDS = [300, 600, 1200, 2400, 4800, 9600, 19200, 28800]
PARITIES = [
    ("E", 1, serial.PARITY_EVEN, serial.STOPBITS_ONE),
    ("N", 1, serial.PARITY_NONE, serial.STOPBITS_ONE),
    ("E", 2, serial.PARITY_EVEN, serial.STOPBITS_TWO),
]

REQUEST = b"/?!\r\n"  # IEC 62056-21 wake-up / request message


def try_one(baud, parity_label, bytesize_label, parity, stopbits):
    try:
        ser = serial.Serial(
            port=PORT,
            baudrate=baud,
            bytesize=serial.SEVENBITS if parity != serial.PARITY_NONE else serial.EIGHTBITS,
            parity=parity,
            stopbits=stopbits,
            timeout=POLL_INTERVAL,
        )
    except Exception as e:
        print(f"[{baud} {parity_label}{bytesize_label}] could not open port: {e}")
        return

    t0 = time.monotonic()
    ser.reset_input_buffer()
    ser.write(REQUEST)
    print(f"\n=== baud={baud} parity={parity_label} stopbits={bytesize_label} "
          f"| sent {REQUEST!r} at t=0.000s, listening {LISTEN_SECONDS}s ===")

    sent_echo_seen = False
    while time.monotonic() - t0 < LISTEN_SECONDS:
        chunk = ser.read(256)
        if chunk:
            t = time.monotonic() - t0
            is_echo = chunk == REQUEST[: len(chunk)] and t < 0.3
            tag = "ECHO-OF-SENT" if is_echo else "** NEW / LATE DATA **"
            if is_echo:
                sent_echo_seen = True
            print(f"  t={t:6.3f}s  [{tag}]  hex={chunk.hex()}  ascii={chunk!r}")
    if not sent_echo_seen:
        print("  (no echo of the sent bytes at all this time)")
    ser.close()


def main():
    for baud in BAUDS:
        for parity_label, bytesize_label, parity, stopbits in PARITIES:
            try_one(baud, parity_label, bytesize_label, parity, stopbits)


if __name__ == "__main__":
    main()
