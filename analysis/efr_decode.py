#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
efr_decode.py  --  Paper C / WS2  --  ONE decoder for EFR/FRE radio telegrams.
===============================================================================
this is grounded directly in the primary standards:

  * DIN 43861-301:2000-04  "Rundsteuerempfaenger Teil 301: Uebertragungsprotokolle Typ A"
  * DIN 43861-302:2000-04  "Rundsteuerempfaenger Teil 302: Uebertragungsprotokolle Typ B"
  * BlinkenCity (38C3, 2024) for the FT1.2 framing + the time-telegram byte layout.

-------------------------------------------------------------------------------
WHAT THE STANDARDS ACTUALLY SAY (and what that means for us)
-------------------------------------------------------------------------------
There are TWO things people lazily call "the EFR telegram", and keeping them
apart is the whole point of being honest in the paper:

  (1) The LINK FRAME we actually capture is an IEC 60870-5-1 **FT1.2** variable
      frame:   68 L L 68 | C | A CI | ...payload... | CS | 16
      Its checksum CS is a simple arithmetic sum  (C + A + CI + sum(payload)) & 0xff.
      We verified this on 830/830 old frames + every fresh MongoDB frame.   [TIER 1]

  (2) INSIDE that FT1.2 payload sits a **DIN 43861-301 Type A data block**:
         [ Funktionsmodus(4) | Adressmodus(4) ]  = payload[0]
         [ Funktions-Spezifikation  0..5 byte ]
         [ Adress-Spezifikation     0..9 byte ]
      (DIN 43861-301, Bild 1, Seite 4.)  The 16 function names we print are copied
      VERBATIM from DIN 43861-301 Section 6 (Typ A) and DIN 43861-302 Section 6
      (Typ B).  I re-checked all 16 of each against the PDF -- they match.   [TIER 1 as NAMES]

The subtle, must-not-overclaim part:
  * DIN 43861-301/302 describe a DIFFERENT *on-air* encoding (bit-packed, with an
    8-bit BCH check, G(x)=x^7+x^6+x^5+x^2+1, "FT2").  Our frames are FT1.2, NOT FT2.
    So the DIN BCH is NOT present in our payload -- that is exactly why an earlier
    BCH bit-alignment search "did not lock": there is no DIN BCH to find.  EFR
    reused the DIN *function semantics* over an FT1.2 *link layer*.             [TIER 2]
  * Because there is no second (DIN) checksum over the data block, we cannot
    checksum-prove the internal field boundaries (where the relay byte sits).
    The frames still parse cleanly to the DIN layout -- see the relay note below --
    but "parses cleanly" is weaker than "checksum-verified".                   [TIER 3/4]

-------------------------------------------------------------------------------
THE RELAY QUESTION (DIN 43861-301, 7.4, Seite 21-22)  --  now DIN-grounded
-------------------------------------------------------------------------------
For EINZELADRESSIERUNG (single receiver, Adressmodus == 0) carrying a Schaltbefehl,
the standard puts, BEFORE the 3-byte Einzeladresse, one extra RELAY byte:

     bit0 = Relais 1 (K1)   bit1 = Relais 2 (K2)   bit2 = Relais 3 (K3)
     bit3 = Relais 4 (K4)   bit4 = Relais 5 (K5)   bit5 = Relais 6 (K6)
     bit6, bit7 = nicht benutzt            (0 = Relais nicht schalten, 1 = schalten)

So a single-address Schaltbefehl payload is:
     [func/mode byte] [ (optional function-spec) ] [relay byte] [addr hi] [addr mid] [addr lo]
i.e. the relay byte is the 4th-from-last payload byte (payload[-4]) whenever the
address is the usual 3 bytes.  Your `eaea` capture parses PERFECTLY this way:

     payload 50 01 00 04 60
       50 -> func=5 (Schaltbefehl AUS), mode=0 (Einzeladressierung)
       01 -> relay byte, bit0 set  -> Relais 1 -> K1
       00 04 60 -> Einzeladresse 0x000460
   => "switch K1 OFF at receiver 0x000460".   Clean, standards-matching hit.    [TIER 3]

Why this is still TIER 3 (strongly supported, not proven):
  * no DIN checksum over the data block to confirm the boundary (see above);
  * only holds for Adressmodus == 0 (single receiver).  Group addressing
    (Ebenen A/B/C/D, mode != 0) carries NO relay byte at all -- it selects whole
    groups, and which physical K reacts is set inside each receiver.           [TIER 4]
  * small sample of real directed commands so far.
The decoder therefore prints the relay reading ONLY with --relays, always labelled
EXPERIMENTAL, and only for mode==0 frames.  Default output makes no relay claim.

-------------------------------------------------------------------------------
USAGE
-------------------------------------------------------------------------------
  Decode one telegram you found (no database needed):
      python3 efr_decode.py --hex 6808086817eaea5001000460a016
      python3 efr_decode.py --hex 6808086817eaea5001000460a016 --relays

  Scan ALL telegrams in Tarik's MongoDB and quantify the Tier-2 patterns:
      ~/.venvs/fre-relay-monitor/bin/python efr_decode.py --scan --out commands.csv

  (reads the DB connection the same way the platform does; or pass --uri "...")

Standard library only, except --scan which needs pymongo (already in the venv).
"""

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

# --------------------------------------------------------------------------- #
#  DIN function tables  --  copied verbatim from the standards, verified pages #
# --------------------------------------------------------------------------- #
# DIN 43861-301:2000, Section 6, Seite 5  (Funktionsmodus -> Funktion), Typ A.
VERSACOM_FN = {
    0x0: "Kalenderzuordnung",                       # 6.1
    0x1: "Parametrierung der Schaltprogramme",      # 6.2
    0x2: "fuer Funktionserweiterung reserviert",    # 6.3
    0x3: "Schaltprogrammwahl",                      # 6.4
    0x4: "Zeitsynchronisation",                     # 6.5
    0x5: "Schaltbefehl AUS",                        # 6.6
    0x6: "Schaltbefehl EIN",                        # 6.7
    0x7: "Herstellerspezifische weitere Funktionen",# 6.8
    0x8: "Wischerbefehl",                           # 6.9
    0x9: "Schaltzyklusart 1",                       # 6.10
    0xA: "Schaltzyklusart 2",                       # 6.11
    0xB: "Ruecksetzen der Zaehlerregister",         # 6.12
    0xC: "Deaktivierung des Empfaengers",           # 6.13
    0xD: "Pruefbefehl",                             # 6.14
    0xE: "Aktivierung des Empfaengers",             # 6.15
    0xF: "Schaltbefehl AUS und Schaltprogrammsperrung",  # 6.16
}
# DIN 43861-301 functions that actually switch an object (carry/accept a relay):
VERSACOM_SWITCHING = {0x5, 0x6, 0x8, 0xF}   # AUS, EIN, Wischer, AUS+Sperrung
# Functions with NO Funktionsspezifikation (payload = func + address only):
VERSACOM_NO_FUNCSPEC = {0x5, 0x6, 0xC, 0xD, 0xE, 0xF}  # 6.6,6.7,6.13,6.14,6.15,6.16

# DIN 43861-302:2000, Section 6, Seite 4  (Funktion -> Bedeutung), Typ B "Semagyr-TOP".
SEMAGYR_FN = {
    0x0: "Herstellerspezifische Funktionserweiterung",  # 6.4.1
    0x1: "Zeit und Datum synchronisieren",              # 6.1.1
    0x2: "Rundsteuer-Tag synchronisieren",              # 6.1.2
    0x3: "Teilprogramme synchronisieren",               # 6.1.3
    0x4: "Eintrag mit Datumsbereich parametrieren",     # 6.2.1
    0x5: "Eintrag ohne Datumsangabe parametrieren",     # 6.2.2
    0x6: "Eintrag loeschen",                            # 6.2.3
    0x7: "Ausfuehrung Eintrag sperren",                 # 6.2.4
    0x8: "Ausfuehrung Eintrag freigeben",               # 6.2.5
    0x9: "Schablone parametrieren",                     # 6.3.1
    0xA: "Timerwert parametrieren",                     # 6.3.2
    0xB: "Merker parametrieren",                        # 6.3.3
    0xC: "fuer Funktionserweiterung reserviert",        # 6.4
    0xD: "fuer Funktionserweiterung reserviert",        # 6.4
    0xE: "fuer Funktionserweiterung reserviert",        # 6.4
    0xF: "fuer Funktionserweiterung reserviert",        # 6.4
}

# EFR weekday field (BlinkenCity 38C3): Sun=0 .. Sat=6
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

BROADCAST_A = 0x0000     # time telegram, read by every receiver
BULK_GROUP_A = 0x2000    # the recurring bulk group stream we see

# Europe/Berlin, for the time-telegram timezone fix (see decode_time).
try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:              # very old python / missing tzdata -> fixed CET fallback
    BERLIN = timezone(timedelta(hours=1))


# --------------------------------------------------------------------------- #
#  FT1.2 frame  (IEC 60870-5-1)  --  TIER 1, verified                          #
# --------------------------------------------------------------------------- #
class Frame:
    """One FT1.2 variable frame:  68 L L 68 | C | A CI | data | CS 16."""

    __slots__ = ("C", "A", "CI", "data", "ck", "length", "raw")

    def __init__(self, C, A, CI, data, ck, length, raw):
        self.C, self.A, self.CI = C, A, CI
        self.data, self.ck, self.length, self.raw = data, ck, length, raw

    # 16-bit "EVU address"  (A = high byte, CI = low byte)
    @property
    def address(self):   return (self.A << 8) | self.CI
    @property
    def ctrl_lo(self):   return self.C & 0x0F      # the "ends in 7" nibble
    @property
    def ctrl_hi(self):   return self.C >> 4
    @property
    def calc_ck(self):   return (self.C + self.A + self.CI + sum(self.data)) & 0xFF
    @property
    def ck_ok(self):     return self.ck == self.calc_ck
    @property
    def len_ok(self):    return self.length == 3 + len(self.data)
    @property
    def is_bcast(self):  return self.address == BROADCAST_A
    @property
    def is_bulk(self):   return self.address == BULK_GROUP_A   # exactly 0x2000
    @property
    def is_group(self):  return self.A == 0x20                 # 0x20xx group family
    @property
    def is_directed(self): return not self.is_bcast and not self.is_group
    @property
    def dhex(self):      return " ".join(f"{b:02x}" for b in self.data)

    # DIN 43861-301 Bild 1: payload[0] = Funktionsmodus(hi) | Adressmodus(lo)
    @property
    def fn_mode(self):   return self.data[0] >> 4 if self.data else None
    @property
    def addr_mode(self): return self.data[0] & 0x0F if self.data else None


def frame_from_hex(hexstr):
    """Parse a '68 L L 68 ... 16' hex string into a Frame, or raise ValueError."""
    raw = bytes.fromhex(hexstr.replace(" ", "").strip())
    if len(raw) < 7 or raw[0] != 0x68 or raw[3] != 0x68:
        raise ValueError("not an FT1.2 frame (needs 68 L L 68 ... CS 16)")
    length = raw[1]
    if raw[1] != raw[2]:
        raise ValueError(f"length bytes differ: {raw[1]:02x} vs {raw[2]:02x}")
    C, A, CI = raw[4], raw[5], raw[6]
    n_data = length - 3                      # control + 2 address bytes already counted
    data = list(raw[7:7 + n_data])
    ck = raw[7 + n_data] if 7 + n_data < len(raw) else raw[-2]
    return Frame(C=C, A=A, CI=CI, data=data, ck=ck, length=length, raw=raw)


# --------------------------------------------------------------------------- #
#  Time telegram  --  TIER 1 (layout verified vs your real frame), + TZ fix    #
# --------------------------------------------------------------------------- #
def decode_time(payload):
    """Decode the 7-byte broadcast time payload (DIN19244 / EFR time telegram).

    Byte layout (BlinkenCity 38C3; verified on our real frame
    00 80 18 0e 93 0b 1a -> 2026-11-19 14:24:32 Thu):
        [0] flags        [1] second<<2   [2] minute     [3] dst<<7 | hour(7)
        [4] weekday<<5 | day(5)          [5] month       [6] year (2000+)

    TIMEZONE FIX: the wall-clock in the telegram is GERMAN LOCAL time (CET/CEST),
    NOT UTC.  Tarik's platform stamped it tzinfo=UTC, which made clock_offset_s
    ~ +3600 s and flagged every time telegram 'implausible'.  Here we localise it
    to Europe/Berlin so the comparison to a UTC receiver clock is correct.
    """
    if len(payload) < 7:
        return None
    second = payload[1] >> 2
    minute = payload[2]
    hour = payload[3] & 0x7F
    dst = bool(payload[3] >> 7)
    day = payload[4] & 0x1F
    weekday_idx = payload[4] >> 5
    month = payload[5]
    year = 2000 + payload[6]
    if not (1 <= month <= 12 and 1 <= day <= 31 and hour < 24 and minute < 60 and second < 60):
        return None
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=BERLIN)
    except ValueError:
        return None
    return {
        "year": year, "month": month, "day": day,
        "weekday": WEEKDAYS[weekday_idx] if weekday_idx < 7 else str(weekday_idx),
        "hour": hour, "minute": minute, "second": second,
        "dst_bit": dst,                              # telegram's own summer-time flag
        "local_iso": local.isoformat(),              # German local time (correct)
        "utc_iso": local.astimezone(timezone.utc).isoformat(),
        "second_provisional": True,                  # seconds field still provisional
    }


# --------------------------------------------------------------------------- #
#  Command classification  --  function name (TIER 1 name) + mode + protocol    #
# --------------------------------------------------------------------------- #
def classify(frame):
    """Return a dict describing the telegram. Naming is DIN-exact; the protocol
    guess and the 'directed->7' hint are heuristics (honestly question-marked)."""
    out = {
        "kind": None, "protocol": None, "function": None, "addr_mode": None,
        "addr_mode_name": None, "confidence": None, "detail": None,
    }
    if frame.is_bcast:
        t = decode_time(frame.data)
        if t:
            out.update(kind="time", protocol="din19244-time",
                       function="Zeit/Datum (broadcast)", confidence="TIER 1",
                       detail=f"{t['local_iso']} (German local)  weekday {t['weekday']}")
            out["time"] = t
            return out
        out.update(kind="broadcast", protocol="unknown",
                   function="broadcast, time payload did not validate", confidence="TIER 1")
        return out

    # command telegram (directed or bulk group)
    f, m = frame.fn_mode, frame.addr_mode
    # Protocol guess: control low-nibble 7 -> Versacom-style directed, else Semagyr-ish.
    # This is the 'ends in 7' heuristic -- TIER 2, NOT proven.  Keep the '?'.
    proto = "versacom?" if frame.ctrl_lo == 0x7 else "semagyr?"
    fn_name = VERSACOM_FN.get(f, "?")
    if f in VERSACOM_SWITCHING:
        fn_name += " [switching]"
    mode_name = ("Einzeladressierung (single receiver)" if m == 0
                 else f"Gruppenadressierung Ebene(n), Adressmodus-Halbbyte={m:04b}")
    out.update(kind="command", protocol=proto, function=fn_name, addr_mode=m,
               addr_mode_name=mode_name, confidence="TIER 1 name / TIER 2 protocol",
               detail=f"Funktionsmodus={f:x}  Adressmodus={m:x}  rest=[{' '.join(f'{b:02x}' for b in frame.data[1:]) or '-'}]")
    return out


# --------------------------------------------------------------------------- #
#  EXPERIMENTAL relay read  --  DIN 43861-301 7.4, Einzeladressierung + Schalt  #
#  TIER 3/4.  Only for Adressmodus==0.  Printed only with --relays.             #
# --------------------------------------------------------------------------- #
def relay_read_experimental(frame):
    """Try to read the DIN 7.4 relay byte. Returns a dict or a reason-string.

    Layout for a single-address switch command (DIN 43861-301, 7.4, Bild Seite 22):
        [func/mode] [ optional func-spec ] [RELAY] [addr-hi] [addr-mid] [addr-lo]
    => with the usual 3-byte Einzeladresse the relay byte is payload[-4].
       bit0->K1, bit1->K2, bit2->K3, bit3->K4, bit4->K5, bit5->K6 ; 1 = switch.
    """
    f, m = frame.fn_mode, frame.addr_mode
    if m != 0:
        return "no relay byte: group addressing (Adressmodus != 0) targets whole groups, not relays"
    if f not in VERSACOM_SWITCHING:
        return f"no relay byte: function {f:x} ({VERSACOM_FN.get(f,'?')}) is not a switching command"
    if len(frame.data) < 5:
        return "payload too short for [func][relay][3-byte address]"
    relay_byte = frame.data[-4]           # 4th from end = relay, if address is 3 bytes
    addr3 = frame.data[-3:]               # 24-bit Einzeladresse (MSB first)
    relays = [f"K{i+1}" for i in range(6) if relay_byte & (1 << i)]
    return {
        "relay_byte": relay_byte,
        "relays": relays,                 # e.g. ['K1']
        "einzeladresse": (addr3[0] << 16) | (addr3[1] << 8) | addr3[2],
        "bits": f"{relay_byte:08b}",
        "note": "EXPERIMENTAL (DIN 7.4 structure; not checksum-proven; mode==0 only)",
    }


# --------------------------------------------------------------------------- #
#  Pretty single-telegram print                                                #
# --------------------------------------------------------------------------- #
def print_one(frame, show_relays=False):
    c = classify(frame)
    typ = ("TIME/broadcast" if frame.is_bcast else
           "GROUP 0x20xx" if frame.is_group else "SINGLE (directed)")
    scope_rule = ("single/broadcast -> low-nibble 7" if frame.ctrl_lo == 0x7
                  else "group-20xx -> low-nibble 0/2/3")
    print("=" * 70)
    print(f"raw        : {' '.join(f'{b:02x}' for b in frame.raw)}")
    print(f"frame (FT1.2): 68 | len {frame.length:02x} | 68 | C={frame.C:02x} "
          f"| EVU={frame.address:04x} | PD=[{frame.dhex}] | CS={frame.ck:02x} | 16")
    print(f"checksum   : {'OK' if frame.ck_ok else 'FAIL'}  "
          f"(calc {frame.calc_ck:02x}, got {frame.ck:02x})   [TIER 1]")
    print(f"length     : {'OK' if frame.len_ok else 'MISMATCH'}   [TIER 1]")
    print(f"type       : {typ}   (control low-nibble = {frame.ctrl_lo:x})")
    print(f"             scope rule [TIER 2, 0 exc / 81398]: {scope_rule}")
    print(f"kind       : {c['kind']}   protocol: {c['protocol']}")
    print(f"function   : {c['function']}   [name = TIER 1, DIN 43861-301/302 Sec.6]")
    if c.get("addr_mode_name"):
        print(f"addressing : {c['addr_mode_name']}")
    if c.get("detail"):
        print(f"detail     : {c['detail']}")
    if c.get("time"):
        t = c["time"]
        print(f"time       : {t['local_iso']}  (German local, Europe/Berlin)")
        print(f"             {t['utc_iso']}  (= UTC)   weekday {t['weekday']}  "
              f"dst-bit={int(t['dst_bit'])}  [seconds provisional]")
    if show_relays:
        r = relay_read_experimental(frame)
        print("-" * 70)
        if isinstance(r, str):
            print(f"relays     : {r}")
        else:
            print(f"relays     : {', '.join(r['relays']) or '(none set)'}   "
                  f"<-- EXPERIMENTAL  [TIER 3/4]")
            print(f"             relay byte {r['relay_byte']:02x} = {r['bits']}  "
                  f"(bit0=K1..bit5=K6)   Einzeladresse 0x{r['einzeladresse']:06x}")
            print(f"             {r['note']}")
    print("=" * 70)


# --------------------------------------------------------------------------- #
#  MongoDB scan  --  read-only; quantify the Tier-2 patterns on the full set    #
# --------------------------------------------------------------------------- #
PLATFORM_ENV_FILES = ["/etc/fre-platform/fre.env",
                      os.path.expanduser("~/Desktop/FRE-Detection-Mitigation/.env")]


def _read_env(path):
    vals = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                v = v[1:-1] if v[:1] in ("'", '"') and v[-1:] == v[:1] else v.split(" #")[0].strip()
                vals[k.strip()] = v
    except OSError:
        pass
    return vals


def _load_uri_db(env_file):
    files = [env_file] if env_file else (
        [os.environ["FRE_ENV_FILE"]] if os.environ.get("FRE_ENV_FILE") else PLATFORM_ENV_FILES)
    s = {}
    for p in files:
        s = _read_env(p)
        if s:
            break
    for k, v in os.environ.items():
        if k.startswith("FRE_"):
            s[k] = v
    uri = s.get("FRE_MONGODB_URI")
    if not uri and s.get("FRE_MONGODB_USERNAME"):
        host = s.get("FRE_MONGODB_HOST", "").removeprefix("mongodb+srv://").strip("/")
        uri = (f"mongodb+srv://{quote_plus(s['FRE_MONGODB_USERNAME'])}:"
               f"{quote_plus(s.get('FRE_MONGODB_PASSWORD',''))}@{host}/?retryWrites=true&w=majority")
    return uri, s.get("FRE_MONGODB_DB", "fre_dataset")


def address_family(fr):
    """Group telegrams by their ADDRESS, not by my old crude directed/bulk split.
      broadcast   : 0x0000            (time telegram)
      group-20xx  : high byte == 0x20 (0x2000..0x20ff -- the EVU group family)
      single      : anything else     (true single-receiver address, e.g. eaea)
    """
    if fr.address == BROADCAST_A:
        return "broadcast"
    if fr.A == 0x20:
        return "group-20xx"
    return "single"


def scan(uri, db_name, collection, out_csv):
    from pymongo import MongoClient
    coll = MongoClient(uri, serverSelectionTimeoutMS=10000)[db_name][collection]

    total = coll.count_documents({})
    print(f"collection {db_name}.{collection}: {total} documents\n")

    table = defaultdict(Counter)    # table[family][ctrl_lo] = count
    highbytes = Counter()           # address high byte (A) -> count
    functions = Counter()
    exc_single = []                 # single address but low-nibble != 7
    exc_group = []                  # group-20xx but low-nibble not in {0,2,3}
    bad_ck = 0
    rows = []

    for doc in coll.find({}):
        hexstr = doc.get("raw_hex")
        if not hexstr:
            continue
        try:
            fr = frame_from_hex(hexstr)
        except ValueError:
            continue
        fam = address_family(fr)
        table[fam][fr.ctrl_lo] += 1
        highbytes[fr.A] += 1
        if not fr.ck_ok:
            bad_ck += 1
        c = classify(fr)
        if fam != "broadcast":       # command telegram
            functions[c["function"]] += 1
            # REFINED hypothesis: single -> low-nibble 7, group-20xx -> {0,2,3}
            if fam == "single" and fr.ctrl_lo != 0x7:
                exc_single.append((f"{fr.address:04x}", f"{fr.C:02x}"))
            if fam == "group-20xx" and fr.ctrl_lo not in (0x0, 0x2, 0x3):
                exc_group.append((f"{fr.address:04x}", f"{fr.C:02x}"))
        # EXPERIMENTAL relay read (DIN 7.4), only for single-address switch commands
        relays_x, relaybyte_x, einzel_x = "", "", ""
        if fam != "broadcast":
            rr = relay_read_experimental(fr)
            if isinstance(rr, dict):
                relays_x = "+".join(rr["relays"])
                relaybyte_x = f"{rr['relay_byte']:02x}"
                einzel_x = f"{rr['einzeladresse']:06x}"
        rows.append([doc.get("received_ts") or doc.get("ts"), f"{fr.address:04x}",
                     f"{fr.C:02x}", fr.ctrl_lo, fam, c["kind"], fr.ck_ok,
                     c["protocol"], c["function"], doc.get("protocol"),
                     relays_x, relaybyte_x, einzel_x])

    # ---- report
    print("CONTROL LOW-NIBBLE  x  ADDRESS FAMILY  (family = by address, not my old split)")
    nibbles = sorted({nb for cc in table.values() for nb in cc})
    print("  family       " + "".join(f"  n={nb:x}" for nb in nibbles) + "     total")
    for fam in ("single", "group-20xx", "broadcast"):
        if fam in table:
            cc = table[fam]
            print(f"  {fam:11} " + "".join(f"{cc.get(nb,0):6}" for nb in nibbles)
                  + f"{sum(cc.values()):10}")

    print("\nADDRESS HIGH-BYTE (A)  --  shows the address space structure:")
    for hb, n in highbytes.most_common(12):
        fam = "broadcast" if hb == 0x00 else "group-20xx" if hb == 0x20 else "single"
        print(f"  A=0x{hb:02x}  {n:7}   ({fam})")

    print(f"\nchecksum failures: {bad_ck} / {total}   [TIER 1 integrity]")

    print("\nREFINED HYPOTHESIS  [TIER 2]:")
    print("   single-address  -> control low-nibble 7")
    print("   group-20xx      -> control low-nibble in {0,2,3}")
    print(f"   exceptions: single!=7 : {len(exc_single)}      group not in {{0,2,3}} : {len(exc_group)}")
    for addr, ctrl in (exc_single[:15] + exc_group[:15]):
        print(f"     EVU {addr}, control {ctrl}")
    print("   NOTE: broadcast (time) telegrams also carry low-nibble 7.")
    print("   NOTE: 'protocol agree' is NOT reported -- our rule and Tarik's are the")
    print("         SAME control-nibble test, so 100% agreement is circular, not proof.")

    print("\nFUNCTION NAMES SEEN (DIN-exact names; counts are observational):")
    for fn, n in functions.most_common(16):
        print(f"  {n:6}  {fn}")

    if out_csv:
        with open(out_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "evu_address", "control", "ctrl_lo", "family", "kind",
                        "checksum_ok", "your_protocol", "function_din", "tarik_protocol",
                        "relay_EXPERIMENTAL", "relay_byte_hex", "einzeladresse_hex"])
            w.writerows(rows)
        print(f"\nwrote {len(rows)} rows -> {out_csv}")


# --------------------------------------------------------------------------- #
LEGEND = """
CONFIDENCE LEGEND (so nobody over-reads this output)
  TIER 1  proven      : FT1.2 frame, arithmetic checksum, time-telegram fields,
                        DIN function *names* (verbatim from DIN 43861-301/302 Sec.6).
  TIER 2  supported   : by ADDRESS FAMILY -- single-address & broadcast -> low-nibble
                        7; group-20xx -> {0,2,3}. (The naive 'directed->7' split was
                        wrong: it mislabelled 0x20xx group frames as directed.)
  TIER 3  clean-parse : DIN 7.4 relay byte for single-address switch commands
                        (payload[-4], bit0=K1..bit5=K6). Parses cleanly (e.g. eaea
                        -> K1) but no DIN checksum over the data block to confirm it.
  TIER 4  speculative : which physical K a GROUP command ends up moving; the full
                        address->plant map. Needs the receiver's IR parametrisation.
"""


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hex", help="decode a single telegram given as hex (68....16)")
    ap.add_argument("--relays", action="store_true",
                    help="also show the EXPERIMENTAL DIN-7.4 relay read (mode==0 only)")
    ap.add_argument("--scan", action="store_true",
                    help="read ALL telegrams from MongoDB and quantify the patterns")
    ap.add_argument("--collection", default="telegrams")
    ap.add_argument("--uri", help="MongoDB URI (else read platform env files)")
    ap.add_argument("--db")
    ap.add_argument("--env-file")
    ap.add_argument("--out", help="with --scan: write every telegram to this CSV")
    ap.add_argument("--legend", action="store_true", help="print the confidence legend and exit")
    args = ap.parse_args()

    if args.legend:
        print(LEGEND); return
    if args.hex:
        try:
            fr = frame_from_hex(args.hex)
        except ValueError as e:
            sys.exit(f"could not parse --hex: {e}")
        print_one(fr, show_relays=args.relays)
        print(LEGEND)
        return
    if args.scan:
        uri, db = _load_uri_db(args.env_file)
        uri, db = (args.uri or uri), (args.db or db)
        if not uri:
            sys.exit("no MongoDB URI (set FRE_MONGODB_URI / platform env, or pass --uri)")
        scan(uri, db, args.collection, args.out)
        print(LEGEND)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
