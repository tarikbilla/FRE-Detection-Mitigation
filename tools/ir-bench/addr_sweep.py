"""IEC 62056-21 sign-on sweep with device-address candidates from the FRE label (read-only).

Usage: addr_sweep.py PORT SERIAL [TYPE]
  SERIAL = the number on the type plate (e.g. the 6 digits of "1 LAN01 00 <serial>")
  TYPE   = the type designation (default LK2-E5-0455)
"""
import serial, sys, time, datetime as dt, pathlib
port, sn = sys.argv[1], sys.argv[2]
typ = sys.argv[3] if len(sys.argv) > 3 else "LK2-E5-0455"
addrs = ["", sn, f"00{sn}", f"0100{sn}", f"LAN0100{sn}", f"1LAN0100{sn}",
         "LAN01", typ[-4:], typ, typ.replace("-", "")]
logdir = pathlib.Path(__file__).resolve().parent / "logs"; logdir.mkdir(exist_ok=True)
log = open(logdir / f"addr_sweep_{dt.datetime.now():%Y%m%d_%H%M%S}.txt", "w")
def p(m): print(m, flush=True); log.write(m + "\n"); log.flush()
def grab(s, sec):
    buf=b""; t0=time.monotonic()
    while time.monotonic()-t0<sec: buf+=s.read(512); time.sleep(0.02)
    return buf
hits = []
for wake in (False, True):
    for a in addrs:
        req = f"/?{a}!\r\n".encode()
        with serial.Serial(port, 300, bytesize=7, parity="E", timeout=0) as s:
            s.reset_input_buffer()
            if wake:   # ~2.1 s of NUL bytes (battery-device wake-up per IEC 62056-21), then 1.5 s pause
                s.write(b"\x00"*60); s.flush(); time.sleep(1.6); s.reset_input_buffer()
            s.write(req); s.flush()
            d = grab(s, 3.0)
        extra = d[len(req):] if d.startswith(req) else d
        tag = "HIT " if extra.strip(b"\x00") else "    "
        p(f"{tag}wake={int(wake)} {req!r:28} echo={d.startswith(req)!s:5} extra={extra!r}")
        if extra.strip(b"\x00"): hits.append((req, extra))
p(f"Hits: {hits}")
