#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fingerprint.py  --  Paper C / WS2  --  physical-layer fingerprint of the signal.
===============================================================================
Read-only.  Reads the MongoDB `observations` collection, auto-discovers every
numeric feature (snr_db, carrier_offset_hz, mark_rel_db, space_rel_db,
fsk_index_db, noise_floor_db, power_dbfs, ...), and summarises its distribution.

WHY (the security point):
  The real transmitter is a distant 100 kW longwave tower (DCF49 Mainflingen).
  Its received fingerprint -- SNR, carrier offset, mark/space levels, FSK index --
  sits in a tight, stable band.  A rogue LOCAL emitter trying to inject a spoofed
  telegram (BlinkenCity Attack: the time telegram has no integrity/replay
  protection) would almost certainly land OUTSIDE that band (wrong carrier offset,
  different SNR/power).  So this baseline is the reference a detector compares
  against.  This script builds the baseline; it does NOT accuse any single frame.

USAGE (on the Pi, venv has pymongo):
  ~/.venvs/fre-relay-monitor/bin/python fingerprint.py --out observations.csv
  (reads the DB connection the same way efr_decode.py does; or pass --uri "...")

Standard library + pymongo only.  No numpy needed.
"""
import argparse
import csv
import math
import os
import sys
from collections import defaultdict

# reuse the exact DB-connection logic from the decoder (same folder)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from efr_decode import _load_uri_db


def flatten_numeric(doc, prefix=""):
    """Yield (dotted_key, float_value) for every numeric leaf in a nested dict."""
    for k, v in doc.items():
        key = f"{prefix}{k}"
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            yield key, float(v)
        elif isinstance(v, dict):
            yield from flatten_numeric(v, key + ".")


def pct(sorted_vals, p):
    """Linear-interpolated percentile p (0..100) of a sorted list."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    i = (len(sorted_vals) - 1) * p / 100.0
    lo, hi = math.floor(i), math.ceil(i)
    if lo == hi:
        return sorted_vals[int(i)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def summarize(name, vals):
    s = sorted(vals)
    n = len(s)
    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n
    std = math.sqrt(var)
    return {
        "feature": name, "n": n, "mean": mean, "std": std,
        "min": s[0], "p5": pct(s, 5), "p25": pct(s, 25), "median": pct(s, 50),
        "p75": pct(s, 75), "p95": pct(s, 95), "max": s[-1],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", default="observations")
    ap.add_argument("--features-key", default="features",
                    help="subdocument holding the numeric features (default: features)")
    ap.add_argument("--channel", help="only this channel, e.g. DCF49 (matches the channel sub-doc)")
    ap.add_argument("--present", action="store_true",
                    help="only observations with signal_state == 'present'")
    ap.add_argument("--query", help="extra MongoDB filter as JSON, merged with --channel/--present")
    ap.add_argument("--uri")
    ap.add_argument("--db")
    ap.add_argument("--env-file")
    ap.add_argument("--limit", type=int, default=0, help="cap documents (0 = all)")
    ap.add_argument("--out", help="write one row per observation to this CSV")
    args = ap.parse_args()

    uri, db = _load_uri_db(args.env_file)
    uri, db = (args.uri or uri), (args.db or db)
    if not uri:
        sys.exit("no MongoDB URI (set FRE_MONGODB_URI / platform env, or pass --uri)")

    from pymongo import MongoClient
    coll = MongoClient(uri, serverSelectionTimeoutMS=10000)[db][args.collection]

    # build the filter (channel sub-field name varies, so match any likely path)
    query = {}
    if args.channel:
        query["$or"] = [{f"channel.{k}": args.channel}
                        for k in ("name", "label", "id", "channel", "station")]
    if args.present:
        query["signal_state"] = "present"
    if args.query:
        import json as _json
        query.update(_json.loads(args.query))

    total = coll.count_documents({})
    matched = coll.count_documents(query) if query else total
    print(f"collection {db}.{args.collection}: {total} documents"
          + (f"  |  filter matched: {matched}" if query else "") + "\n")
    if query and matched == 0:
        sys.exit("filter matched 0 documents -- check the channel name / field. "
                 "Run once with no filter and tell me the channel sub-doc fields.")

    series = defaultdict(list)       # feature -> [values]
    rows = []
    all_keys = []
    cursor = coll.find(query)
    if args.limit:
        cursor = cursor.limit(args.limit)

    for doc in cursor:
        feats = doc.get(args.features_key)
        src = feats if isinstance(feats, dict) else doc   # fall back to top level
        pairs = dict(flatten_numeric(src))
        for k, v in pairs.items():
            series[k].append(v)
        if args.out:
            rows.append((doc.get("_id"), doc.get("received_ts") or doc.get("ts"), pairs))
            for k in pairs:
                if k not in all_keys:
                    all_keys.append(k)

    if not series:
        sys.exit(f"no numeric features found under '{args.features_key}' or top level. "
                 f"Try  --features-key <name>  (check one doc's field names first).")

    # ---- report
    cols = ["feature", "n", "mean", "std", "min", "p5", "p25", "median", "p75", "p95", "max"]
    print("PHYSICAL-LAYER FINGERPRINT  (one row per numeric feature)")
    print("  " + "  ".join(f"{c:>9}" if c != "feature" else f"{c:<22}" for c in cols))
    for name in sorted(series):
        d = summarize(name, series[name])
        line = f"  {d['feature']:<22}" + "  ".join(
            f"{d[c]:9.0f}" if c == "n" else f"{d[c]:9.2f}" for c in cols[1:])
        print(line)

    print("\nHOW TO READ THIS (TIER 1 = measured; interpretation is TIER 2):")
    print("  * a TIGHT band (small std, p5..p95 close to median) = stable, single")
    print("    distant source -> good baseline for 'normal'.")
    print("  * carrier_offset_hz should cluster near a fixed value; a frame far from")
    print("    it is a candidate rogue/local emitter.")
    print("  * compare authentic vs any suspect frames against THIS band, per feature.")

    if args.out:
        with open(args.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["_id", "ts"] + all_keys)
            for _id, ts, pairs in rows:
                w.writerow([_id, ts] + [pairs.get(k, "") for k in all_keys])
        print(f"\nwrote {len(rows)} rows x {len(all_keys)} features -> {args.out}")


if __name__ == "__main__":
    main()
