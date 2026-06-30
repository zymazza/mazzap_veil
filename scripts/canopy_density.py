#!/usr/bin/env python3
"""Canopy density per pipeline run — the temporal query the store exists for.

For every pipeline run, reports the parcel tree population as it stood after
that run: tree count and summed crown area (pi*r^2 from each tree's latest
radius observation as of that run). Membership and liveness are evaluated
per-run, so the output is the answer to "when did canopy density change".

An optional bounding box (scene-local meters, x=east y=north) restricts the
question to part of the parcel, e.g. the north field:

  python3 scripts/canopy_density.py                      # whole parcel
  python3 scripts/canopy_density.py --bbox -340 200 340 450   # north field
  python3 scripts/canopy_density.py --member member_surrounding
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import twin_store

QUERY = """
WITH runs AS (
  SELECT run_id, script, started_at FROM pipeline_runs
),
state AS (
  SELECT r.run_id, e.entity_id,
    (SELECT o.value FROM observations o
      WHERE o.entity_id = e.entity_id AND o.attr = :member AND o.run_id <= r.run_id
      ORDER BY o.obs_id DESC LIMIT 1) AS member,
    (SELECT CAST(o.value AS REAL) FROM observations o
      WHERE o.entity_id = e.entity_id AND o.attr = 'radius' AND o.run_id <= r.run_id
      ORDER BY o.obs_id DESC LIMIT 1) AS radius
  FROM runs r
  JOIN entities e ON e.kind = 'tree'
    AND e.created_run_id <= r.run_id
    AND (e.retired_run_id IS NULL OR e.retired_run_id > r.run_id)
  JOIN trees t ON t.entity_id = e.entity_id
  WHERE t.x BETWEEN :minx AND :maxx AND t.y BETWEEN :miny AND :maxy
)
SELECT r.run_id, r.script, r.started_at,
       COUNT(*) AS tree_count,
       CAST(ROUND(SUM(3.14159265 * radius * radius), 0) AS INTEGER) AS crown_area_m2
FROM state s JOIN runs r USING (run_id)
WHERE s.member = 'true'
GROUP BY r.run_id
ORDER BY r.started_at
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("MINX", "MINY", "MAXX", "MAXY"),
                    help="restrict to a scene-local bounding box (meters)")
    ap.add_argument("--member", default="member_parcel",
                    choices=["member_parcel", "member_surrounding"])
    args = ap.parse_args()
    minx, miny, maxx, maxy = args.bbox or (-1e9, -1e9, 1e9, 1e9)

    conn = sqlite3.connect(twin_store.STORE_PATH)
    rows = conn.execute(QUERY, {"member": args.member, "minx": minx,
                                "maxx": maxx, "miny": miny, "maxy": maxy}).fetchall()
    scope = f"bbox ({minx:g},{miny:g})..({maxx:g},{maxy:g})" if args.bbox else "whole area"
    print(f"{args.member}, {scope}\n")
    print(f"{'run':>4}  {'started_at':20}  {'script':38}  {'trees':>7}  {'crown m^2':>10}  {'delta':>7}")
    prev = None
    for run_id, script, started, count, area in rows:
        delta = "" if prev is None else f"{count - prev:+d}"
        print(f"{run_id:>4}  {started:20}  {script:38}  {count:>7}  {area:>10}  {delta:>7}")
        prev = count
    conn.close()


if __name__ == "__main__":
    main()
