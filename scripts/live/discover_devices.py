#!/usr/bin/env python3
"""Discover local devices that can be used as live telemetry gateways."""

from __future__ import annotations

import argparse
import asyncio
import json


def discover_serial() -> dict:
    from serial.tools import list_ports

    devices = []
    for port in list_ports.comports():
        if (
            not port.vid and not port.pid and not port.manufacturer
            and (port.hwid or "").lower() == "n/a"
            and (port.description or "").lower() == "n/a"
        ):
            continue
        text = " ".join(str(x or "") for x in (
            port.device, port.name, port.description, port.manufacturer,
            port.product, port.hwid,
        ))
        devices.append({
            "id": port.device,
            "address": port.device,
            "label": port.description or port.device,
            "description": port.description,
            "manufacturer": port.manufacturer,
            "product": port.product,
            "serial_number": port.serial_number,
            "vid": port.vid,
            "pid": port.pid,
            "hwid": port.hwid,
            "candidate": any(s in text.lower() for s in ("meshtastic", "nrf52", "seeed", "t1000")),
        })
    return {"transport": "serial", "devices": devices}


async def discover_bluetooth(timeout: float) -> dict:
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for address, pair in found.items():
        device, adv = pair
        name = device.name or adv.local_name or ""
        service_uuids = sorted(str(u) for u in (adv.service_uuids or []))
        text = " ".join([name, address, " ".join(service_uuids)]).lower()
        devices.append({
            "id": address,
            "address": address,
            "label": name or address,
            "name": name,
            "rssi": getattr(adv, "rssi", None),
            "service_uuids": service_uuids,
            "candidate": "meshtastic" in text,
        })
    devices.sort(key=lambda d: (not d["candidate"], d.get("label") or d["address"]))
    return {"transport": "bluetooth", "devices": devices}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["serial", "bluetooth"], required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()
    if args.transport == "serial":
      print(json.dumps(discover_serial()))
    else:
      print(json.dumps(asyncio.run(discover_bluetooth(args.timeout))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
