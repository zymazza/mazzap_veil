#!/usr/bin/env python3
"""Live telemetry side store and twin-store export helper.

The Node server writes every normalized live event to JSONL and calls this
helper to mirror events into data/live/telemetry.sqlite. The SQLite file is a
private, day-indexed replay database, separate from twin.gpkg.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
PROJECT = os.path.dirname(SCRIPTS)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
DATA_DIR = os.path.abspath(os.environ.get("TWIN_DATA_DIR") or os.path.join(PROJECT, "data"))
LIVE_DIR = os.path.join(DATA_DIR, "live")
DB_PATH = os.path.join(LIVE_DIR, "telemetry.sqlite")
REGISTRY_PATH = os.path.join(LIVE_DIR, "registry.json")
EVENTS_PATH = os.path.join(LIVE_DIR, "events.jsonl")
COMMAND_DIR = os.path.join(LIVE_DIR, "commands")
LIVE_POSITION_ACTIVE_SECONDS = int(os.environ.get("VEIL_LIVE_POSITION_ACTIVE_SECONDS") or 2 * 60)
LIVE_POSITION_STALE_SECONDS = int(os.environ.get("VEIL_LIVE_POSITION_STALE_SECONDS") or 15 * 60)
BUSY_TIMEOUT_MS = int(os.environ.get("VEIL_LIVE_DB_BUSY_TIMEOUT_MS") or 5000)
# Bump when migrate_event_timestamp_columns() changes. Gates the one-time backfill
# so it can't run on every connect (which made per-event ingestion O(n^2)).
SCHEMA_VERSION = 1


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_day TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    device_id TEXT NOT NULL,
    gateway_id TEXT,
    label TEXT,
    lat REAL,
    lon REAL,
    event_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_events_day ON events(observed_day, observed_at);
CREATE INDEX IF NOT EXISTS idx_live_events_device ON events(device_id, observed_at);
CREATE TABLE IF NOT EXISTS exports (
    export_id INTEGER PRIMARY KEY AUTOINCREMENT,
    exported_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    filters_json TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    device_count INTEGER NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_timestamp(dt: datetime) -> tuple[str, int]:
    canonical = dt.astimezone(timezone.utc).replace(microsecond=0)
    return canonical.strftime("%Y-%m-%dT%H:%M:%SZ"), int(canonical.timestamp() * 1000)


def normalize_timestamp(value, field_name: str) -> tuple[str, int]:
    if value is None or value == "":
        return canonical_timestamp(datetime.now(timezone.utc))
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO timestamp string")
    text = value.strip()
    if not re.search(r"(Z|[+-]\d{2}:\d{2})$", text):
        raise ValueError(f"{field_name} must include an explicit timezone")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include an explicit timezone")
    return canonical_timestamp(parsed)


def day_of(ts: str) -> str:
    canonical, _ms = normalize_timestamp(ts, "observed_at")
    return canonical[:10]


def migrate_event_timestamp_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "observed_at_ms" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN observed_at_ms INTEGER")
    if "received_at_ms" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN received_at_ms INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_events_day_ms ON events(observed_day, observed_at_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_live_events_device_ms ON events(device_id, observed_at_ms)")

    rows = conn.execute(
        "SELECT id, observed_day, observed_at, received_at, event_json,"
        " observed_at_ms, received_at_ms FROM events"
        " WHERE observed_at_ms IS NULL OR received_at_ms IS NULL"
        " OR observed_at NOT LIKE '____-__-__T__:__:__Z'"
        " OR received_at NOT LIKE '____-__-__T__:__:__Z'"
    ).fetchall()
    for row in rows:
        event = None
        try:
            parsed = json.loads(row["event_json"] or "{}")
            if isinstance(parsed, dict):
                event = parsed
        except Exception:
            event = None

        observed_raw = row["observed_at"] or (event or {}).get("observed_at")
        received_raw = row["received_at"] or (event or {}).get("received_at")
        try:
            observed_at, observed_at_ms = normalize_timestamp(observed_raw, "observed_at")
            received_at, received_at_ms = normalize_timestamp(received_raw, "received_at")
        except ValueError:
            continue

        event_json = row["event_json"]
        if event is not None:
            event["observed_at"] = observed_at
            event["received_at"] = received_at
            event_json = json.dumps(event, separators=(",", ":"), sort_keys=True)

        conn.execute(
            "UPDATE events SET observed_day = ?, observed_at = ?, observed_at_ms = ?,"
            " received_at = ?, received_at_ms = ?, event_json = ? WHERE id = ?",
            (observed_at[:10], observed_at, observed_at_ms, received_at, received_at_ms, event_json, row["id"]),
        )


def connect() -> sqlite3.Connection:
    os.makedirs(LIVE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    # The Node server spawns one `live_store.py append` process per event, so
    # several writers (plus an `export` reader) can hit the DB concurrently.
    # WAL lets readers and a writer coexist; busy_timeout makes a second writer
    # wait-and-retry instead of failing immediately with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA)
    # Run the backfill/index migration only when the DB predates the current
    # schema version. It full-table-scans events (the NULL / NOT-LIKE filter has
    # no usable index), and the server spawns one append process per event, so
    # running it on every connect turned ingestion into O(n^2). user_version is 0
    # on fresh and pre-gating DBs, so the migration still runs exactly once.
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        migrate_event_timestamp_columns(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


def parse_ts(value: str | None):
    if not value:
        return None
    try:
        text = str(value)
        if not re.search(r"(Z|[+-]\d{2}:\d{2})$", text):
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def timestamp_ms(value: str | None) -> int | None:
    try:
        _canonical, ms = normalize_timestamp(value, "timestamp")
        return ms
    except ValueError:
        return None


def safe_device_key(value) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:!@-]+", "_", str(value or ""))[:120]


def clean_live_color(value):
    text = str(value or "").strip()
    return text.lower() if re.match(r"^#[0-9a-fA-F]{6}$", text) else None


def read_json_file(path: str, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def write_json_file_atomic(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2)
    os.replace(tmp, path)


def live_registry() -> dict:
    doc = read_json_file(REGISTRY_PATH, None)
    if isinstance(doc, dict):
        doc["gateways"] = doc["gateways"] if isinstance(doc.get("gateways"), list) else []
        doc["devices"] = doc["devices"] if isinstance(doc.get("devices"), dict) else {}
        return doc
    return {"version": 1, "gateways": [], "devices": {}}


def save_live_registry(doc: dict) -> None:
    doc["version"] = 1
    doc["updated_at"] = utcnow()
    write_json_file_atomic(REGISTRY_PATH, doc)


def live_token() -> str | None:
    env_token = os.environ.get("VEIL_LIVE_TOKEN", "").strip()
    if env_token:
        return env_token
    for path in (os.path.join(DATA_DIR, ".live_token"), os.path.join(PROJECT, ".live_token")):
        try:
            token = open(path, encoding="utf-8").read().strip()
            if token:
                return token
        except Exception:
            pass
    return None


def local_live_url() -> str:
    explicit = os.environ.get("VEIL_SELF_URL") or os.environ.get("VEIL_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('PORT') or 4173}"


def live_api(path: str, method: str = "GET", payload: dict | None = None,
             query: dict | None = None, timeout: float = 10.0) -> dict:
    base = local_live_url()
    qs = urlencode({k: v for k, v in (query or {}).items() if v is not None}, doseq=True)
    url = base + path + (("?" + qs) if qs else "")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = live_token()
    if token:
        headers["X-VEIL-Live-Token"] = token
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8")
    except HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(detail)
            msg = parsed.get("error") or parsed
        except Exception:
            msg = detail.strip() or e.reason
        return {"ok": False, "error": msg, "status": e.code, "url": url}
    except URLError as e:
        return {"ok": False, "error": f"live API unavailable: {e.reason}", "url": url}
    except Exception as e:
        return {"ok": False, "error": str(e), "url": url}
    try:
        parsed = json.loads(body or "{}")
    except Exception:
        return {"ok": False, "error": "live API returned non-JSON", "body": body[:500], "url": url}
    if isinstance(parsed, dict):
        parsed.setdefault("ok", True)
    return parsed


def latest_events_from_db(limit: int = 2000) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT event_json FROM events ORDER BY observed_at_ms DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    events = []
    for row in reversed(rows):
        try:
            events.append(json.loads(row["event_json"]))
        except Exception:
            pass
    return events


def latest_events_from_jsonl(limit: int = 2000) -> list[dict]:
    try:
        with open(EVENTS_PATH, encoding="utf-8") as fh:
            lines = fh.readlines()[-max(1, int(limit)):]
    except Exception:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


def merge_latest_event(previous: dict, event: dict, pref: dict) -> dict:
    merged = {**previous, **event}
    if "position" not in event and previous.get("position"):
        merged["position"] = previous["position"]
    if "motion" not in event and previous.get("motion"):
        merged["motion"] = previous["motion"]
    merged["label"] = pref.get("label") or event.get("label") or previous.get("label") or event.get("device_id")
    merged["color"] = clean_live_color(pref.get("color")) or previous.get("color") or event.get("color")
    merged["visible"] = pref.get("visible") is not False
    merged["last_event_observed_at"] = event.get("observed_at")
    merged["last_event_received_at"] = event.get("received_at")
    if event.get("position"):
        merged["position_observed_at"] = event.get("observed_at")
        merged["position_received_at"] = event.get("received_at")
    else:
        merged["position_observed_at"] = previous.get("position_observed_at") or previous.get("observed_at")
        merged["position_received_at"] = previous.get("position_received_at") or previous.get("received_at")
    return merged


def device_freshness(event: dict, pref: dict | None = None, now=None) -> dict:
    pref = pref or {}
    now = now or datetime.now(timezone.utc)
    position_at = parse_ts(event.get("position_observed_at") or event.get("observed_at"))
    last_packet_at = parse_ts(event.get("last_event_received_at") or event.get("received_at"))
    age_seconds = None if position_at is None else max(0, round((now - position_at).total_seconds()))
    last_packet_age_seconds = None if last_packet_at is None else max(0, round((now - last_packet_at).total_seconds()))
    state = "active"
    reason = "location is current"
    if not event.get("position"):
        state = "no_location"
        reason = "no location packet has been received"
    elif age_seconds is None or age_seconds > LIVE_POSITION_STALE_SECONDS:
        state = "offline"
        reason = "location is too old"
    elif age_seconds > LIVE_POSITION_ACTIVE_SECONDS:
        state = "stale"
        reason = "location has not updated recently"
    gateway_id = safe_device_key(pref.get("gateway_id") or (event.get("link") or {}).get("gateway_id"))
    return {
        "state": state,
        "active": state == "active",
        "stale": state != "active",
        "reason": reason,
        "gateway_id": gateway_id or None,
        "gateway_state": None,
        "position_observed_at": event.get("position_observed_at"),
        "position_received_at": event.get("position_received_at"),
        "age_seconds": age_seconds,
        "active_after_seconds": LIVE_POSITION_ACTIVE_SECONDS,
        "offline_after_seconds": LIVE_POSITION_STALE_SECONDS,
        "last_event_observed_at": event.get("last_event_observed_at") or event.get("observed_at"),
        "last_event_received_at": event.get("last_event_received_at") or event.get("received_at"),
        "last_packet_age_seconds": last_packet_age_seconds,
    }


def fallback_live_snapshot() -> dict:
    reg = live_registry()
    latest = {}
    for event in latest_events_from_db() or latest_events_from_jsonl():
        device_id = safe_device_key(event.get("device_id"))
        if not device_id:
            continue
        pref = reg.get("devices", {}).get(device_id, {})
        latest[device_id] = merge_latest_event(latest.get(device_id, {}), event, pref)
    now = datetime.now(timezone.utc)
    devices = []
    for device_id, event in latest.items():
        pref = reg.get("devices", {}).get(device_id, {})
        devices.append({
            **event,
            "label": pref.get("label") or event.get("label") or device_id,
            "color": clean_live_color(pref.get("color")) or event.get("color"),
            "visible": pref.get("visible") is not False,
            "freshness": device_freshness(event, pref, now),
        })
    devices.sort(key=lambda e: timestamp_ms(e.get("last_event_observed_at") or e.get("observed_at")) or -1, reverse=True)
    return {
        "schema": "veil.live.snapshot.v1",
        "updated_at": utcnow(),
        "source": "telemetry.sqlite" if os.path.exists(DB_PATH) else "events.jsonl",
        "note": "Bridge process state is only available when the VEIL HTTP server is running.",
        "gateways": [{**g, "bridge": {"state": "unknown"}} for g in reg.get("gateways", [])],
        "devices": devices,
        "preferences": reg.get("devices", {}),
    }


def telemetry_snapshot(include_hidden: bool = False, prefer_live_api: bool = True) -> dict:
    """Return current live device/gateway state, using the Node process manager when reachable."""
    if prefer_live_api:
        api = live_api("/api/live/latest", timeout=2.5)
        if api.get("ok") and api.get("schema"):
            if not include_hidden:
                api["devices"] = [d for d in api.get("devices", []) if d.get("visible") is not False]
            api["source"] = "live_api"
            return api
    snap = fallback_live_snapshot()
    if not include_hidden:
        snap["devices"] = [d for d in snap.get("devices", []) if d.get("visible") is not False]
    return snap


def telemetry_days() -> list[str]:
    days = set()
    daily_dir = os.path.join(LIVE_DIR, "daily")
    try:
        for name in os.listdir(daily_dir):
            m = re.match(r"^(\d{4}-\d{2}-\d{2})\.jsonl$", name)
            if m:
                days.add(m.group(1))
    except Exception:
        pass
    try:
        with connect() as conn:
            for row in conn.execute("SELECT DISTINCT observed_day FROM events ORDER BY observed_day"):
                days.add(row["observed_day"])
    except Exception:
        pass
    return sorted(days)


def telemetry_history(date: str | None = None, dates: list[str] | None = None,
                      device_ids: list[str] | None = None, kind: str | None = None,
                      since: str | None = None, until: str | None = None,
                      limit: int = 200) -> dict:
    """Read events from the temporary telemetry SQLite store."""
    valid_dates = [d for d in (dates or ([] if not date else [date])) if isinstance(d, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", d)]
    valid_devices = [safe_device_key(d) for d in (device_ids or []) if safe_device_key(d)]
    max_limit = max(1, min(int(limit or 200), 2000))
    params = []
    where = []
    if valid_dates:
        where.append("observed_day IN (%s)" % ",".join("?" for _ in valid_dates))
        params.extend(valid_dates)
    if valid_devices:
        where.append("device_id IN (%s)" % ",".join("?" for _ in valid_devices))
        params.extend(valid_devices)
    if kind:
        where.append("kind = ?")
        params.append(str(kind))
    if since:
        where.append("observed_at_ms >= ?")
        params.append(normalize_timestamp(since, "since")[1])
    if until:
        where.append("observed_at_ms <= ?")
        params.append(normalize_timestamp(until, "until")[1])
    sql = "SELECT id, observed_day, observed_at, received_at, kind, device_id, gateway_id, label, lat, lon, event_json FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY observed_at_ms DESC, id DESC LIMIT ?"
    params.append(max_limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    events = []
    for row in rows:
        event = json.loads(row["event_json"])
        events.append(event)
    events.reverse()
    return {
        "ok": True,
        "store": DB_PATH,
        "filters": {
            "dates": valid_dates,
            "device_ids": valid_devices,
            "kind": kind,
            "since": since,
            "until": until,
            "limit": max_limit,
        },
        "event_count": len(events),
        "events": events,
    }


def telemetry_store_summary() -> dict:
    """Summarize the temporary live telemetry data store."""
    with connect() as conn:
        totals = conn.execute(
            "SELECT COUNT(*) AS events, COUNT(DISTINCT device_id) AS devices,"
            " MIN(observed_at_ms) AS first_observed_at_ms, MAX(observed_at_ms) AS last_observed_at_ms FROM events"
        ).fetchone()
        first = conn.execute(
            "SELECT observed_at FROM events WHERE observed_at_ms = ? ORDER BY id ASC LIMIT 1",
            (totals["first_observed_at_ms"],),
        ).fetchone() if totals["first_observed_at_ms"] is not None else None
        last = conn.execute(
            "SELECT observed_at FROM events WHERE observed_at_ms = ? ORDER BY id DESC LIMIT 1",
            (totals["last_observed_at_ms"],),
        ).fetchone() if totals["last_observed_at_ms"] is not None else None
        by_day = [dict(r) for r in conn.execute(
            "SELECT observed_day AS date, COUNT(*) AS event_count, COUNT(DISTINCT device_id) AS device_count"
            " FROM events GROUP BY observed_day ORDER BY observed_day"
        )]
        by_kind = [dict(r) for r in conn.execute(
            "SELECT kind, COUNT(*) AS event_count FROM events GROUP BY kind ORDER BY event_count DESC, kind"
        )]
        by_device = [dict(r) for r in conn.execute(
            "SELECT device_id, MAX(label) AS label, COUNT(*) AS event_count,"
            " MAX(observed_at_ms) AS last_observed_at_ms FROM events GROUP BY device_id"
            " ORDER BY last_observed_at_ms DESC LIMIT 100"
        )]
        for device in by_device:
            row = conn.execute(
                "SELECT observed_at FROM events WHERE device_id = ? AND observed_at_ms = ?"
                " ORDER BY id DESC LIMIT 1",
                (device["device_id"], device["last_observed_at_ms"]),
            ).fetchone()
            device["last_observed_at"] = row["observed_at"] if row else None
            device.pop("last_observed_at_ms", None)
        exports = [dict(r) for r in conn.execute(
            "SELECT export_id, exported_at, mode, filters_json, event_count, device_count"
            " FROM exports ORDER BY exported_at DESC LIMIT 20"
        )]
    for row in exports:
        try:
            row["filters"] = json.loads(row.pop("filters_json") or "{}")
        except Exception:
            row["filters"] = {}
    return {
        "ok": True,
        "store": DB_PATH,
        "days": telemetry_days(),
        "event_count": totals["events"] or 0,
        "device_count": totals["devices"] or 0,
        "first_observed_at": first["observed_at"] if first else None,
        "last_observed_at": last["observed_at"] if last else None,
        "by_day": by_day,
        "by_kind": by_kind,
        "recent_devices": by_device,
        "recent_exports": exports,
    }


def register_gateway(gateway_id: str | None = None, name: str | None = None,
                     protocol: str = "meshtastic", transport: str = "bluetooth",
                     address: str | None = None, node_id: str | None = None,
                     connect_bridge: bool = False) -> dict:
    payload = {
        "gateway_id": gateway_id,
        "name": name,
        "protocol": protocol,
        "transport": transport,
        "address": address,
        "node_id": node_id,
        "connect": connect_bridge,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    api = live_api("/api/live/gateways", method="POST", payload=payload, timeout=15.0)
    if api.get("ok"):
        return api
    # Fallback can register metadata but cannot start a process.
    if connect_bridge:
        return api
    reg = live_registry()
    now = utcnow()
    candidate_id = safe_device_key(gateway_id or name or f"gateway-{int(datetime.now().timestamp())}")
    gateway = {
        "id": candidate_id,
        "name": str(name or candidate_id)[:120],
        "protocol": str(protocol or "meshtastic").lower(),
        "transport": str(transport or "bluetooth").lower(),
        "address": str(address or "")[:200],
        "created_at": now,
        "updated_at": now,
    }
    if node_id:
        gateway["node_id"] = safe_device_key(node_id)
    idx = next((i for i, g in enumerate(reg["gateways"]) if g.get("id") == candidate_id), -1)
    if idx >= 0:
        gateway["created_at"] = reg["gateways"][idx].get("created_at") or now
        reg["gateways"][idx] = {**reg["gateways"][idx], **gateway}
    else:
        reg["gateways"].append(gateway)
    save_live_registry(reg)
    return {"ok": True, "gateway": gateway, "registry": reg, "source": "registry_file"}


def manage_gateway(action: str, gateway_id: str | None = None, name: str | None = None,
                   protocol: str = "meshtastic", transport: str = "bluetooth",
                   address: str | None = None, node_id: str | None = None) -> dict:
    action = str(action or "").lower()
    if action in ("register", "connect"):
        return register_gateway(gateway_id, name, protocol, transport, address, node_id, connect_bridge=action == "connect")
    if action in ("start", "restart"):
        gid = safe_device_key(gateway_id)
        if not gid:
            return {"ok": False, "error": "gateway_id is required"}
        return live_api("/api/live/gateways/restart", method="POST", payload={"gateway_id": gid}, timeout=15.0)
    if action == "stop":
        gid = safe_device_key(gateway_id)
        if not gid:
            return {"ok": False, "error": "gateway_id is required"}
        return live_api("/api/live/gateways/stop", method="POST", payload={"gateway_id": gid}, timeout=10.0)
    if action == "remove":
        gid = safe_device_key(gateway_id)
        if not gid:
            return {"ok": False, "error": "gateway_id is required"}
        return live_api("/api/live/gateways/remove", method="POST", payload={"gateway_id": gid}, timeout=10.0)
    return {"ok": False, "error": f"unsupported action: {action}", "valid_actions": ["register", "connect", "start", "restart", "stop", "remove"]}


def queue_device_command(device_id: str, gateway_id: str, command: str = "request_position",
                         channel_index: int = 0, hop_limit: int | None = None) -> dict:
    device_id = safe_device_key(device_id)
    gateway_id = safe_device_key(gateway_id)
    command = str(command or "request_position").lower()
    if not device_id:
        return {"ok": False, "error": "device_id is required"}
    if not gateway_id:
        return {"ok": False, "error": "gateway_id is required"}
    if command not in ("request_position", "traceroute"):
        return {"ok": False, "error": f"unsupported command: {command}", "valid_commands": ["request_position", "traceroute"]}
    payload = {
        "device_id": device_id,
        "gateway_id": gateway_id,
        "command": command,
        "channel_index": int(channel_index or 0),
        "hop_limit": hop_limit,
    }
    api = live_api("/api/live/devices/command", method="POST", payload=payload, timeout=10.0)
    if api.get("ok"):
        return api
    if api.get("status") == 409:
        return api
    os.makedirs(COMMAND_DIR, exist_ok=True)
    cmd = {
        "id": f"{int(datetime.now().timestamp() * 1000)}-{os.getpid()}",
        "command": command,
        "device_id": device_id,
        "gateway_id": gateway_id,
        "channel_index": int(channel_index or 0),
        "hop_limit": hop_limit,
        "queued_at": utcnow(),
    }
    with open(os.path.join(COMMAND_DIR, f"{gateway_id}.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(cmd, separators=(",", ":")) + "\n")
    return {"ok": True, "queued": cmd, "source": "command_file", "warning": api.get("error")}


def manage_device(action: str, device_id: str, gateway_id: str | None = None,
                  label: str | None = None, visible: bool | None = None,
                  color: str | None = None, command: str | None = None,
                  channel_index: int = 0, hop_limit: int | None = None) -> dict:
    action = str(action or "update").lower()
    did = safe_device_key(device_id)
    if not did:
        return {"ok": False, "error": "device_id is required"}
    if action == "update":
        payload = {"device_id": did}
        if label is not None:
            payload["label"] = str(label)[:120]
        if visible is not None:
            payload["visible"] = bool(visible)
        if color is not None:
            cleaned = clean_live_color(color)
            if not cleaned:
                return {"ok": False, "error": "color must be #rrggbb"}
            payload["color"] = cleaned
        if gateway_id is not None:
            payload["gateway_id"] = safe_device_key(gateway_id)
        api = live_api("/api/live/devices", method="POST", payload=payload, timeout=10.0)
        if api.get("ok"):
            return api
        reg = live_registry()
        pref = reg["devices"].get(did, {})
        if "label" in payload:
            pref["label"] = payload["label"]
        if "visible" in payload:
            pref["visible"] = payload["visible"]
        if "color" in payload:
            pref["color"] = payload["color"]
        if "gateway_id" in payload:
            pref["gateway_id"] = payload["gateway_id"]
        reg["devices"][did] = pref
        save_live_registry(reg)
        return {"ok": True, "device": {"device_id": did, **pref}, "source": "registry_file", "warning": api.get("error")}
    if action == "remove":
        api = live_api("/api/live/devices/remove", method="POST", payload={"device_id": did}, timeout=10.0)
        if api.get("ok"):
            return api
        reg = live_registry()
        existed = did in reg["devices"]
        reg["devices"].pop(did, None)
        save_live_registry(reg)
        return {"ok": True, "removed": 1 if existed else 0, "source": "registry_file", "warning": api.get("error")}
    if action in ("request_position", "traceroute", "command"):
        return queue_device_command(did, gateway_id or "", command or ("traceroute" if action == "traceroute" else "request_position"), channel_index, hop_limit)
    return {"ok": False, "error": f"unsupported action: {action}", "valid_actions": ["update", "remove", "request_position", "traceroute", "command"]}


def discover_connections(transport: str = "serial", timeout: float | None = None) -> dict:
    transport = str(transport or "serial").lower()
    if transport not in ("serial", "bluetooth"):
        return {"ok": False, "error": "transport must be serial or bluetooth"}
    query = {"transport": transport}
    api = live_api("/api/live/discover", method="GET", query=query, timeout=20.0 if transport == "bluetooth" else 8.0)
    if api.get("ok"):
        return api
    try:
        if transport == "serial":
            from discover_devices import discover_serial

            return {"ok": True, **discover_serial(), "source": "local_python"}
        import asyncio
        from discover_devices import discover_bluetooth

        return {"ok": True, **asyncio.run(discover_bluetooth(float(timeout or 8.0))), "source": "local_python"}
    except Exception as e:
        return {"ok": False, "error": str(e), "live_api_error": api.get("error")}


def append_event(event: dict) -> None:
    now = utcnow()
    received_raw = event.get("received_at") or now
    observed_raw = event.get("observed_at") or received_raw
    observed_at, observed_at_ms = normalize_timestamp(observed_raw, "observed_at")
    received_at, received_at_ms = normalize_timestamp(received_raw, "received_at")
    event = {
        **event,
        "observed_at": observed_at,
        "received_at": received_at,
    }
    pos = event.get("position") or {}
    link = event.get("link") or {}
    with connect() as conn:
        conn.execute(
            "INSERT INTO events"
            " (observed_day, observed_at, observed_at_ms, received_at, received_at_ms, kind, device_id, gateway_id,"
            " label, lat, lon, event_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                observed_at[:10],
                observed_at,
                observed_at_ms,
                received_at,
                received_at_ms,
                event.get("kind") or "data",
                event.get("device_id"),
                link.get("gateway_id"),
                event.get("label"),
                pos.get("lat"),
                pos.get("lon"),
                json.dumps(event, separators=(",", ":"), sort_keys=True),
            ),
        )


def selected_events(filters: dict) -> list[dict]:
    dates = filters.get("dates") or ([filters["date"]] if filters.get("date") else [])
    dates = [d for d in dates if isinstance(d, str) and len(d) == 10]
    device_ids = filters.get("device_ids") or []
    params = []
    where = []
    if dates:
        where.append("observed_day IN (%s)" % ",".join("?" for _ in dates))
        params.extend(dates)
    if device_ids:
        where.append("device_id IN (%s)" % ",".join("?" for _ in device_ids))
        params.extend(device_ids)
    sql = "SELECT event_json, observed_at_ms FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY observed_at_ms, id"
    with connect() as conn:
        rows = [(json.loads(r["event_json"]), r["observed_at_ms"]) for r in conn.execute(sql, params)]

    mode = filters.get("mode") or "day"
    if mode != "snapshot":
        return [event for event, _observed_at_ms in rows]
    at = filters.get("at")
    at_ms = normalize_timestamp(at, "at")[1] if at else None
    latest = {}
    for event, observed_at_ms in rows:
        if at_ms is not None and (observed_at_ms is None or observed_at_ms > at_ms):
            continue
        latest[event["device_id"]] = event
    return list(latest.values())


def scene_xy(lon: float, lat: float) -> tuple[float, float]:
    from pyproj import Transformer

    georef_path = os.path.join(DATA_DIR, "georef.json")
    with open(georef_path) as fh:
        georef = json.load(fh)
    origin = georef["origin_utm"]
    crs = georef.get("analysis_crs") or "EPSG:26918"
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    easting, northing = transformer.transform(float(lon), float(lat))
    return round(easting - origin[0], 3), round(northing - origin[1], 3)


def live_entity_id(device_id: str) -> str:
    digest = hashlib.sha1(device_id.encode("utf-8")).hexdigest()[:12]
    return f"live_device:{digest}"


def observe_twin_once(store, twin_store, eid: str, attr: str, value, run_id: int,
                      source: str | None = None, observed_at: str | None = None,
                      dedup: bool = True) -> bool:
    """Write a live observation unless this exact event was already exported."""
    encoded = twin_store.encode_value(value)
    row = store.conn.execute(
        "SELECT 1 FROM observations"
        " WHERE entity_id = ? AND attr = ? AND value = ? AND observed_at = ?"
        " AND (source = ? OR (source IS NULL AND ? IS NULL))"
        " LIMIT 1",
        (eid, attr, encoded, observed_at, source, source),
    ).fetchone()
    if row is not None:
        return False
    return store.observe(eid, attr, value, run_id, source=source,
                         observed_at=observed_at, dedup=dedup)


def export_to_twin(filters: dict) -> dict:
    sys.path.insert(0, SCRIPTS)
    import twin_store
    from twin_store import Store

    events = [e for e in selected_events(filters) if (e.get("position") or {}).get("lat") is not None]
    if not events:
        return {"ok": True, "event_count": 0, "device_count": 0}

    with Store() as store:
        store.ensure_spatial_layer(
            "live_devices",
            "POINT",
            "entity_id TEXT UNIQUE, source TEXT, x REAL, y REAL, properties TEXT",
        )
        run = store.begin_run(
            "live_store.py export",
            notes=json.dumps({k: filters.get(k) for k in ("mode", "date", "dates", "device_ids", "at")}),
        )
        seen = set()
        for event in events:
            device_id = event["device_id"]
            eid = live_entity_id(device_id)
            pos = event.get("position") or {}
            x, y = scene_xy(pos["lon"], pos["lat"])
            source = (event.get("source") or {}).get("protocol") or "live"
            props = {
                "device_id": device_id,
                "label": event.get("label") or device_id,
                "kind": event.get("kind"),
                "source": event.get("source") or {},
            }
            store.upsert_entity(eid, "live_device", run, observed_at=event.get("observed_at"))
            store.conn.execute(
                "INSERT INTO live_devices (entity_id, source, x, y, properties, geom)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(entity_id) DO UPDATE SET"
                " source = excluded.source, x = excluded.x, y = excluded.y,"
                " properties = excluded.properties, geom = excluded.geom",
                (eid, source, x, y, twin_store.encode_value(props), twin_store.gpkg_point_blob(x, y)),
            )
            observed_at = event.get("observed_at")
            observe_twin_once(store, twin_store, eid, "device_id", device_id, run,
                              source=source, observed_at=observed_at)
            observe_twin_once(store, twin_store, eid, "label", event.get("label") or device_id, run,
                              source=source, observed_at=observed_at)
            observe_twin_once(store, twin_store, eid, "position", event.get("position"), run,
                              source=source, observed_at=observed_at, dedup=False)
            observe_twin_once(store, twin_store, eid, "live_event", event, run,
                              source=source, observed_at=observed_at, dedup=False)
            if event.get("message"):
                observe_twin_once(store, twin_store, eid, "message", event["message"], run,
                                  source=source, observed_at=observed_at, dedup=False)
            if "data" in event:
                observe_twin_once(store, twin_store, eid, "data", event["data"], run,
                                  source=source, observed_at=observed_at, dedup=False)
            seen.add(device_id)
        store.finish_run(run)
        store.conn.commit()

    with connect() as conn:
        conn.execute(
            "INSERT INTO exports (exported_at, mode, filters_json, event_count, device_count)"
            " VALUES (?, ?, ?, ?, ?)",
            (utcnow(), filters.get("mode") or "day", json.dumps(filters, sort_keys=True), len(events), len(seen)),
        )
    return {"ok": True, "event_count": len(events), "device_count": len(seen)}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "append"
    payload = json.loads(sys.stdin.read() or "{}")
    if cmd == "append":
        append_event(payload)
        print(json.dumps({"ok": True}))
    elif cmd == "export":
        print(json.dumps(export_to_twin(payload)))
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
