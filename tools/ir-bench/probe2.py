import serial, time, sys
port = sys.argv[1]
def grab(s, sec):
    buf=b""; t0=time.monotonic()
    while time.monotonic()-t0<sec: buf+=s.read(512); time.sleep(0.02)
    return buf
print("== long passive listen ==")
for baud,bs,par,sec in ((9600,8,"N",15),(300,7,"E",8),(19200,8,"N",8),(28800,8,"N",5),(1200,7,"E",5)):
    with serial.Serial(port,baud,bytesize=bs,parity=par,timeout=0) as s:
        s.reset_input_buffer(); d=grab(s,sec)
        print(f"{baud:5} {bs}{par}1 {sec}s -> {len(d)} B {d[:80]!r}")
print("== sign-on at higher bauds ==")
for baud in (19200,28800):
    for bs,par in ((7,"E"),(8,"N")):
        with serial.Serial(port,baud,bytesize=bs,parity=par,timeout=0) as s:
            req=b"/?!\r\n"; s.reset_input_buffer(); s.write(req); s.flush(); d=grab(s,2.5)
            echo=d.startswith(req); extra=d[len(req):] if echo else d
            print(f"{baud:5} {bs}{par}1 echo={echo} extra={extra!r}")
