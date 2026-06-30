"""National LANDFIRE EVT value->name+physiognomy table, fetched on demand.

The repo ships no data: the ~1k-row CONUS attribute table is downloaded once
from LANDFIRE and cached (gitignored) the first time it's needed, then read
from the cache. Both the vegetation hook and the fetcher use this.
"""

import csv
import io
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache", "landfire_evt_vat.json")
CSV_URL = "https://landfire.gov/sites/default/files/CSV/2024/LF2024_EVT.csv"


def _raw():
    if os.path.exists(CACHE):
        return json.load(open(CACHE))
    print("downloading the national LANDFIRE EVT attribute table (one-time)…")
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "veil/1.0"})
    data = urllib.request.urlopen(req, timeout=120).read()
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace"))))
    raw = {}
    for r in rows:
        v = r["VALUE"].strip()
        if v.lstrip("-").isdigit() and int(v) >= 0:
            raw[v] = {"name": (r.get("EVT_NAME") or "").strip(),
                      "phys": (r.get("EVT_PHYS") or "").strip()}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(raw, open(CACHE, "w"))
    return raw


def load_vat():
    """{evt_code: (EVT_NAME, EVT_PHYS)} for all CONUS codes."""
    return {int(c): (d["name"], d["phys"]) for c, d in _raw().items()}
