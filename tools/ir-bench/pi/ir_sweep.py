#!/usr/bin/env python3
"""
ir_sweep.py -- automated baud/parity sweep over the IR read/write head.
Sends the standard IEC 62056-21 "wake up" request and logs ANY bytes
that come back, even a single stray byte, for every setting tried.
Run on the PC (COM3) or the BeagleBone (/dev/ttyUSB0).
"""
import serial
import time

PORT = "/dev/ttyUSB0"   # change to "COM3" on Windows
BAUDS = [300, 600, 1200, 2400, 4800, 9600, 19200, 28800]
PARITIES = [serial.PARITY_EVEN, serial.PARITY_NONE, serial.PARITY_ODD]
PARITY_NAME = {serial.PARITY_EVEN: "E", serial.PARITY_NONE: "N", serial.PARITY_ODD: "O"}

# IEC 62056-21 standard request message: "/?!\r\n"
REQUEST = b"/?!\r\n"

def try_setting(baud, parity, stopbits=serial.STOPBITS_ONE, bytesize=serial.SEVENBITS):
    try:
        with serial.Serial(PORT, baudrate=baud, parity=parity,
                            stopbits=stopbits, bytesize=bytesize,
                            timeout=2) as ser:
            ser.reset_input_buffer()
            ser.write(REQUEST)
            time.sleep(0.5)
            resp = ser.read(256)
            return resp
    except Exception as e:
        return f"ERROR: {e}"

def main():
    print(f"Sweeping {PORT} ... sending {REQUEST!r} at each setting")
    for baud in BAUDS:
        for parity in PARITIES:
            for bytesize in [serial.SEVENBITS, serial.EIGHTBITS]:
                resp = try_setting(baud, parity, bytesize=bytesize)
                tag = f"{baud:6d} baud  {bytesize}{PARITY_NAME[parity]}1"
                if isinstance(resp, str):
                    print(f"  {tag}: {resp}")
                elif resp:
                    print(f"  {tag}: GOT {len(resp)} bytes -> {resp!r}")
                # silence (empty resp) is NOT printed, to keep the log readable;
                # uncomment the next line if you want to see every attempt:
                # else: print(f"  {tag}: (no response)")

if __name__ == "__main__":
    main()
