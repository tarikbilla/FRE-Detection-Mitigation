"""Continuous passive IR listen with timestamps. Usage: listen.py PORT SECONDS [BAUD] [7E1|8N1]"""
import serial, sys, time, datetime as dt, pathlib
port, secs = sys.argv[1], float(sys.argv[2])
baud = int(sys.argv[3]) if len(sys.argv) > 3 else 9600
fmt = sys.argv[4] if len(sys.argv) > 4 else "8N1"
bs, par = int(fmt[0]), fmt[1]
logdir = pathlib.Path(__file__).resolve().parent / "logs"; logdir.mkdir(exist_ok=True)
ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
out = open(logdir / f"listen_{ts}_{baud}{fmt}.txt", "w"); raw = open(logdir / f"listen_{ts}_{baud}{fmt}.bin", "wb")
def p(m):
    line = f"{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {m}"; print(line, flush=True); out.write(line + "\n"); out.flush()
p(f"Listening {secs:.0f}s on {port} @ {baud} {fmt} -> {out.name}")
total = 0; t0 = time.monotonic(); last_tick = 0
with serial.Serial(port, baud, bytesize=bs, parity=par, timeout=0) as s:
    s.reset_input_buffer()
    while (el := time.monotonic() - t0) < secs:
        d = s.read(4096)
        if d:
            total += len(d); raw.write(d); raw.flush()
            p(f"RX {len(d):4} B  hex: {d.hex(' ')}")
            p(f"           text: {d.decode('latin-1')!r}")
        if int(el) // 10 > last_tick:
            last_tick = int(el) // 10; p(f"... {int(el)}s elapsed, {total} B so far")
        time.sleep(0.01)
p(f"Done. Total {total} bytes.")
