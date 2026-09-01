#!/usr/bin/env python3
"""Validate data/data.json for the Standards Tracker.

Fails the build (non-zero exit) on the failure modes the tracker cannot absorb:
malformed JSON, a bad date format, or a new update missing a required field or a
source link. Run locally the same way CI does:  python3 .github/validate_data.py
"""
import json, re, sys

ERRORS = []
def err(msg): ERRORS.append(msg)

DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")   # YYYY-MM or YYYY-MM-DD

try:
    with open("data/data.json", encoding="utf-8") as f:
        d = json.load(f)
except Exception as e:
    print("FAIL: data/data.json is not valid JSON:", e)
    sys.exit(1)

# meta.last_updated present and well-formed
meta = d.get("meta", {})
lu = meta.get("last_updated")
if not lu or not DATE_RE.match(str(lu)):
    err(f"meta.last_updated missing or badly formatted: {lu!r}")

# updates: required fields + source, valid date
UPDATE_FIELDS = ["date","framework","workstream","headline","detail","so_what",
                 "significance","source_url","source_title","source_publisher","last_verified"]
for i, u in enumerate(d.get("updates", [])):
    for fld in UPDATE_FIELDS:
        if not u.get(fld):
            err(f"updates[{i}] ({u.get('headline','?')[:40]}): missing '{fld}'")
    for df in ("date","last_verified"):
        if u.get(df) and not DATE_RE.match(str(u[df])):
            err(f"updates[{i}]: bad {df} '{u.get(df)}' (need YYYY-MM or YYYY-MM-DD)")
    su = str(u.get("source_url",""))
    if su and not su.startswith("http"):
        err(f"updates[{i}]: source_url is not a URL: {su!r}")

# every dated field across the dataset must be YYYY-MM or YYYY-MM-DD
for m in d.get("timeline_milestones", []):
    if m.get("date") and not DATE_RE.match(str(m["date"])):
        err(f"timeline_milestones: bad date '{m.get('date')}' for {m.get('label','?')[:40]}")
    # deadline = somebody must act by this date. update = a dated event with
    # nothing to file. The rail and the calendar export both depend on it.
    if m.get("kind") not in ("deadline", "update"):
        err(f"timeline_milestones: kind must be 'deadline' or 'update', got {m.get('kind')!r} "
            f"for {m.get('label','?')[:40]}")

if ERRORS:
    print(f"FAIL: {len(ERRORS)} validation error(s):")
    for e in ERRORS:
        print("  -", e)
    sys.exit(1)

print(f"OK: data.json valid. last_updated={lu}, "
      f"{len(d.get('updates',[]))} updates, "
      f"{len(d.get('timeline_milestones',[]))} milestones.")
