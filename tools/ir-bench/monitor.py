"""Long-running read-only IR monitor for the FRE (Langmatz EK 393).
Cycle: IEC 62056-21 sign-on @300 7E1 -> if ident, full readout (option 0) -> passive listen @9600 8N1.
Usage: monitor.py PORT SECONDS"""
import serial, sys, time, datetime as dt, pathlib
port, secs = sys.argv[1], float(sys.argv[2])
BAUD = {"0":300,"1":600,"2":1200,"3":2400,"4":4800,"5":9600,"6":19200}
logdir = pathlib.Path(__file__).resolve().parent / "logs"; logdir.mkdir(exist_ok=True)
stamp = f"{dt.datetime.now():%Y%m%d_%H%M%S}"
log = open(logdir / f"monitor_{stamp}.txt", "w"); raw = open(logdir / f"monitor_{stamp}.bin", "wb")
def p(m):
    line = f"{dt.datetime.now():%H:%M:%S}  {m}"; print(line, flush=True); log.write(line+"\n"); log.flush()
def grab(s, overall, quiet=None):
    buf=b""; t0=last=time.monotonic()
    while time.monotonic()-t0 < overall:
        d = s.read(4096)
        if d: buf += d; last = time.monotonic()
        elif quiet and buf and time.monotonic()-last > quiet: break
        time.sleep(0.02)
    return buf
def rx(label, d):
    raw.write(d); raw.flush()
    p(f"*** {label} RX {len(d)} B hex: {d.hex(' ')}"); p(f"    text: {d.decode('latin-1')!r}")

req = b"/?!\r\n"; t0 = time.monotonic(); cycles = 0; hits = 0; last_readout = -1e9
p(f"Monitor started: {secs:.0f}s on {port}")
while time.monotonic() - t0 < secs:
    cycles += 1
    s = serial.Serial(port, 300, bytesize=7, parity="E", timeout=0)
    try:
        s.reset_input_buffer(); s.write(req); s.flush()
        d = grab(s, 3.0, quiet=1.8)
        echo = d.startswith(req); extra = d[len(req):] if echo else d
        if extra.strip(b"\x00"):
            hits += 1; rx("[sign-on reply]", extra)
            ident = extra.strip(b"\r\n\x00")
            if ident.startswith(b"/") and len(ident) >= 5 and time.monotonic()-last_readout > 120:
                zc = chr(ident[4]); baud = BAUD.get(zc, 300)
                ack = b"\x06" + b"0" + zc.encode() + b"0\r\n"
                s.write(ack); s.flush(); time.sleep(0.3); s.baudrate = baud; s.reset_input_buffer()
                data = grab(s, 60.0, quiet=2.5)
                if data.startswith(ack): data = data[len(ack):]
                rx(f"[READOUT @ {baud}]", data); last_readout = time.monotonic()
                if b"\x02" in data:
                    body = data[data.index(b"\x02")+1:].split(b"\x03")[0]
                    for line in body.decode("ascii","replace").splitlines():
                        if line.strip(): p(f"    | {line}")
        elif not echo and cycles % 10 == 1:
            p("WARNING: no echo from head - is it still attached?")
    finally:
        s.close()
    with serial.Serial(port, 9600, bytesize=8, parity="N", timeout=0) as s2:
        s2.reset_input_buffer(); d = grab(s2, 7.0)
        if d: hits += 1; rx("[passive 9600 8N1]", d)
    if cycles % 30 == 0:
        p(f"... {int(time.monotonic()-t0)}s elapsed, {cycles} cycles, {hits} responses so far")
p(f"Monitor finished: {cycles} cycles, {hits} responses.")
