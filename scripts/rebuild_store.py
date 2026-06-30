#!/usr/bin/env python3
"""Rebuild data/twin.gpkg from the append-only write journal (data/journal/).

The journal is the canonical history; the gpkg is a materialized index of it.
(Both are private/gitignored, inside the twin's data dir — never committed to
this repo.) Replaying reproduces the store exactly — same runs, same
timestamps, same observation order (and therefore the same latest-per-attr
state). Use after carrying a twin's data dir to a new machine, or to restore the
journaled truth if the gpkg is lost, corrupted, or ahead of the journal after a
crashed run.

The existing gpkg (if any) is kept as data/twin.gpkg.bak.

Run:  python3 scripts/rebuild_store.py   (npm run rebuild-store)
"""

import glob
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_store
from twin_store import Store

REBUILD_PATH = twin_store.STORE_PATH + ".rebuilding"


def journal_files():
    return sorted(glob.glob(os.path.join(twin_store.JOURNAL_DIR, "*.jsonl.gz")))


def main():
    files = journal_files()
    if not files:
        sys.exit("no journal files in data/journal/ — run `npm run migrate` to "
                 "create the store (and journal) from the flat bundle first")

    if os.path.exists(REBUILD_PATH):
        os.remove(REBUILD_PATH)
    store = Store(REBUILD_PATH, journal=False)
    total_ops = 0
    for path in files:
        n = 0
        with gzip.open(path, "rt") as fh:
            for line in fh:
                store.apply_journal_op(json.loads(line))
                n += 1
        store.conn.commit()
        print(f"replayed {os.path.basename(path)}: {n} ops")
        total_ops += n

    runs = store.conn.execute(
        "SELECT run_id, script, started_at FROM pipeline_runs ORDER BY run_id"
    ).fetchall()
    n_entities = store.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    n_obs = store.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    store.close()

    if os.path.exists(twin_store.STORE_PATH):
        os.replace(twin_store.STORE_PATH, twin_store.STORE_PATH + ".bak")
    os.replace(REBUILD_PATH, twin_store.STORE_PATH)

    print(f"\nrebuilt {os.path.relpath(twin_store.STORE_PATH, twin_store.PROJECT)}: "
          f"{total_ops} ops -> {len(runs)} runs, {n_entities} entities, {n_obs} observations")
    for run_id, script, started in runs:
        print(f"  run {run_id}  {started}  {script}")


if __name__ == "__main__":
    main()
