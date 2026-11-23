"""Send the IEC 62056-21 sign-on repeatedly while you rotate/shift the IR head.
Stops and prints the identification as soon as the FRE answers. Read-only.
Usage: rotate_scan.py PORT [SECONDS]"""
import serial, sys, time, datetime as dt, pathlib
port = sys.argv[1]; secs = float(sys.argv[2]) if len(sys.argv) > 2 else 180
logdir = pathlib.Path(__file__).resolve().parent / "logs"; logdir.mkdir(exist_ok=True)
log = open(logdir / f"rotate_scan_{dt.datetime.now():%Y%m%d_%H%M%S}.txt", "w")
def p(m):
    line = f"{dt.datetime.now():%H:%M:%S}  {m}"; print(line, flush=True); log.write(line + "\n"); log.flush()
def grab(s, sec):
    buf=b""; t0=time.monotonic()
    while time.monotonic()-t0<sec: buf+=s.read(512); time.sleep(0.02)
    return buf
req = b"/?!\r\n"; t0 = time.monotonic(); n = 0
p(f"Scanning for {secs:.0f}s - rotate the head 90 deg every ~15 s, then try 180 deg (cable up).")
while time.monotonic() - t0 < secs:
    for baud in (300, 1200, 9600):
        with serial.Serial(port, baud, bytesize=7, parity="E", timeout=0) as s:
            s.reset_input_buffer(); s.write(req); s.flush(); d = grab(s, 2.0)
        n += 1
        echo = d.startswith(req); extra = d[len(req):] if echo else d
        if extra.strip(b"\x00"):
            p(f"*** RESPONSE @ {baud} 7E1: {extra!r}  hex: {extra.hex(' ')}")
            if b"/" in extra:
                p("Identification received - keep the head in this position and run fre_reader.py"); sys.exit(0)
        elif n % 6 == 0:
            p(f"... {int(time.monotonic()-t0)}s, {n} requests, echo={'yes' if echo else 'NO'}, no answer yet")
p("No answer in this run.")
