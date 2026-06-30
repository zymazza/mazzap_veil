#!/usr/bin/env python3
"""Ingest viewer building-placement saves into the twin store.

server.js appends one JSON line per "Save Transform" to
data/buildings/models/placements.log.jsonl ({ts, placements}). That log file
is the Node -> Python handoff: the zero-dependency server never touches the
gpkg. Each new line becomes one 'placement' observation per building (the
whole placement dict as one value), timestamped with the save time. A meta
cursor (lines already ingested) makes re-runs idempotent.

Run:  python3 scripts/ingest_placements.py   (also called by the exporter)
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_store
from twin_store import Store

LOG_PATH = os.path.join(twin_store.DATA_DIR, "buildings", "models",
                        "placements.log.jsonl")
CURSOR_KEY = "placements_log_lines_ingested"


def ingest(store, log_path=LOG_PATH):
    """Returns the number of placement observations written. log_path defaults
    to the repo twin's log; pass a twin's own log for an alternate data dir."""
    if not os.path.exists(log_path):
        return 0
    with open(log_path) as fh:
        lines = [l for l in fh.read().splitlines() if l.strip()]
    done = store.get_meta(CURSOR_KEY, 0)
    new = lines[done:]
    if not new:
        return 0
    run = store.begin_run("ingest_placements.py",
                          notes=f"{len(new)} new log lines")
    wrote = 0
    for line in new:
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # malformed line still advances the cursor
        ts = rec.get("ts")
        for bid, placement in (rec.get("placements") or {}).items():
            eid = f"building_model:{bid}"
            store.upsert_entity(eid, "building_model", run)
            if store.observe(eid, "placement", placement, run,
                             source="viewer_editor", observed_at=ts):
                wrote += 1
    store.set_meta(CURSOR_KEY, len(lines))
    store.finish_run(run)
    store.conn.commit()
    return wrote


def main():
    with Store() as store:
        wrote = ingest(store)
    print(f"placements log: {wrote} new placement observations")


if __name__ == "__main__":
    main()
