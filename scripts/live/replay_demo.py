#!/usr/bin/env python3
"""Post a small moving demo device into VEIL live telemetry."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone

import requests


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def live_events_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/api/live/events"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--veil", default=os.environ.get("VEIL_URL", "http://127.0.0.1:4173"))
    parser.add_argument("--token", default=os.environ.get("VEIL_LIVE_TOKEN"))
    parser.add_argument("--device-id", default="demo-tracker")
    parser.add_argument("--label", default="Demo tracker")
    parser.add_argument("--lat", type=float, default=39.9806)
    parser.add_argument("--lon", type=float, default=-105.2705)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-VEIL-Live-Token"] = args.token
    for i in range(args.count):
        a = i / max(1, args.count - 1) * math.tau
        event = {
            "schema": "veil.live.v1",
            "kind": "position",
            "device_id": args.device_id,
            "label": args.label,
            "observed_at": utcnow(),
            "position": {
                "lat": args.lat + math.sin(a) * 0.00035,
                "lon": args.lon + math.cos(a) * 0.00045,
                "alt_m": None,
                "accuracy_m": 8,
            },
            "motion": {"speed_mps": 1.4, "heading_deg": (i * 12) % 360},
            "link": {"gateway_id": "demo-gateway", "snr_db": 8.5, "rssi_dbm": -92, "hops": 0},
            "source": {"protocol": "meshtastic", "transport": "replay"},
        }
        res = requests.post(live_events_url(args.veil), headers=headers, data=json.dumps(event), timeout=10)
        res.raise_for_status()
        print(f"posted {i + 1}/{args.count}")
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
