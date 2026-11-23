import serial, time, sys
port = sys.argv[1]
reqs = [b"/?!\r\n", b"/?00000000!\r\n", b"\x00"*20 + b"/?!\r\n"]
for baud in (300, 2400, 4800, 9600):
    for bs, par in ((7, "E"), (8, "N")):
        for req in reqs:
            with serial.Serial(port, baud, bytesize=bs, parity=par, timeout=0) as s:
                s.reset_input_buffer(); s.write(req); s.flush()
                buf = b""; t0 = time.monotonic()
                while time.monotonic() - t0 < 2.5:
                    buf += s.read(256); time.sleep(0.02)
                extra = buf[len(req):] if buf.startswith(req) else buf
                echo = "echo" if buf.startswith(req) else "    "
                print(f"{baud:5} {bs}{par}1 {req[-14:]!r:22} {echo} extra={extra!r}")
