#!/usr/bin/env python3
# refine.py -- re-cut commands.csv by ADDRESS FAMILY and test the refined rule.
# Reads the commands.csv that efr_decode.py --scan already wrote. No database.
import csv, sys
from collections import Counter, defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "commands.csv"
table = defaultdict(Counter)      # family -> {ctrl_lo: count}
highbytes = Counter()
exc_single, exc_group = [], []
n = 0

def family(addr):
    if addr == 0x0000:      return "broadcast"
    if (addr >> 8) == 0x20: return "group-20xx"   # 0x2000..0x20ff
    return "single"

with open(path, newline="") as fh:
    for row in csv.DictReader(fh):
        n += 1
        addr = int(row["evu_address"], 16)
        lo = int(row["ctrl_lo"])
        fam = family(addr)
        table[fam][lo] += 1
        highbytes[addr >> 8] += 1
        if fam == "single" and lo != 0x7:
            exc_single.append((row["evu_address"], row["control"]))
        if fam == "group-20xx" and lo not in (0x0, 0x2, 0x3):
            exc_group.append((row["evu_address"], row["control"]))

print(f"rows: {n}\n")
print("CONTROL LOW-NIBBLE  x  ADDRESS FAMILY")
nibbles = sorted({nb for c in table.values() for nb in c})
print("  family       " + "".join(f"  n={nb:x}" for nb in nibbles) + "     total")
for fam in ("single", "group-20xx", "broadcast"):
    if fam in table:
        c = table[fam]
        print(f"  {fam:11} " + "".join(f"{c.get(nb,0):6}" for nb in nibbles)
              + f"{sum(c.values()):10}")

print("\nADDRESS HIGH-BYTE (A):")
for hb, cnt in highbytes.most_common(12):
    fam = "broadcast" if hb == 0 else "group-20xx" if hb == 0x20 else "single"
    print(f"  A=0x{hb:02x}  {cnt:7}   ({fam})")

print("\nREFINED RULE:  single -> low-nibble 7,   group-20xx -> {0,2,3}")
print(f"  exceptions:  single != 7 : {len(exc_single)}     group not in {{0,2,3}} : {len(exc_group)}")
for addr, ctrl in (exc_single[:20] + exc_group[:20]):
    print(f"    EVU {addr}, control {ctrl}")
