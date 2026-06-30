#!/usr/bin/env python3
"""Bridge Meshtastic packets into VEIL live telemetry.

Examples:
  python3 scripts/live/meshtastic_serial_bridge.py --transport serial --port /dev/ttyUSB0
  python3 scripts/live/meshtastic_serial_bridge.py --transport bluetooth --address AA:BB:CC:DD:EE:FF
  python3 scripts/live/meshtastic_serial_bridge.py --transport internet --host 192.168.1.42
"""

from __future__ import annotations

import argparse
import atexit
import concurrent.futures
import json
import math
import queue
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import requests
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from meshtastic.protobuf import mesh_pb2, portnums_pb2, telemetry_pb2
from pubsub import pub

RECONNECT_BASE_SECONDS = 2.0
RECONNECT_MAX_SECONDS = 30.0
HEALTH_CHECK_SECONDS = 15.0
POST_QUEUE_MAX_SIZE = 1000
POST_MAX_ATTEMPTS = 5
POST_BACKOFF_BASE_SECONDS = 1.0
POST_BACKOFF_MAX_SECONDS = 30.0
NODE_NUM_MAX = 0xFFFFFFFF
NODE_ID_HEX = re.compile(r"^!([0-9a-fA-F]{1,8})$")
NODE_ID_0X_HEX = re.compile(r"^0x([0-9a-fA-F]{1,8})$")
NODE_ID_BARE_HEX = re.compile(r"^[0-9a-fA-F]{1,8}$")


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_value(source, key):
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _node_num(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= NODE_NUM_MAX else None
    text = str(value).strip()
    if not text:
        return None
    match = NODE_ID_HEX.match(text) or NODE_ID_0X_HEX.match(text)
    if match:
        return int(match.group(1), 16)
    if text.isdigit():
        num = int(text, 10)
        return num if 0 <= num <= NODE_NUM_MAX else None
    if NODE_ID_BARE_HEX.match(text) and any(c.isalpha() for c in text):
        return int(text, 16)
    return None


def _node_candidates(value):
    if value is None:
        return
    if isinstance(value, dict) or not isinstance(value, (str, int, float, bool)):
        for key in ("my_node_num", "myNodeNum", "node_num", "nodeNum", "num", "id", "fromId", "from"):
            candidate = _get_value(value, key)
            if candidate is not None:
                yield candidate
        user = _get_value(value, "user")
        user_id = _get_value(user, "id") if user is not None else None
        if user_id is not None:
            yield user_id
    else:
        yield value


def node_id(*values) -> str:
    fallback = None
    for value in values:
        for candidate in _node_candidates(value):
            num = _node_num(candidate)
            if num is not None:
                return f"!{num:08x}"
            if fallback is None and candidate is not None:
                text = str(candidate).strip()
                if text:
                    fallback = text
    return fallback or "unknown"


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_heading(value):
    heading = as_float(value)
    if heading is None:
        return None
    if 0 <= heading <= 360:
        return heading
    if float(heading).is_integer() and 100000 <= heading <= 360 * 100000:
        return heading / 100000.0
    return None


def first_coord(pos: dict, decimal_key: str, integer_key: str):
    value = pos.get(decimal_key)
    if value is not None:
        return as_float(value)
    value = pos.get(integer_key)
    if value is not None:
        return as_float(value / 1e7)
    return None


def decoded_payload(decoded: dict):
    payload = decoded.get("payload")
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    return None


def portnum_name(decoded: dict) -> str:
    value = decoded.get("portnum")
    if isinstance(value, str):
        return value.split(".")[-1]
    if isinstance(value, int):
        try:
            return portnums_pb2.PortNum.Name(value)
        except ValueError:
            return str(value)
    return ""


def parse_message(proto_cls, payload: bytes) -> dict | None:
    if not payload:
        return None
    msg = proto_cls()
    try:
        msg.ParseFromString(payload)
    except Exception:
        return None
    return MessageToDict(msg, preserving_proto_field_name=True)


def message_to_dict(value):
    if isinstance(value, Message):
        return MessageToDict(value, preserving_proto_field_name=True)
    return value


def json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Message):
        return json_safe(message_to_dict(value))
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value)}
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)


def post_event(args, event: dict) -> None:
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["X-VEIL-Live-Token"] = args.token
    url = args.veil.rstrip("/") + "/api/live/events"
    res = requests.post(url, headers=headers, data=json.dumps(json_safe(event)), timeout=10)
    res.raise_for_status()


class EventPoster:
    def __init__(
        self,
        args,
        maxsize: int = POST_QUEUE_MAX_SIZE,
        max_attempts: int = POST_MAX_ATTEMPTS,
        backoff_base: float = POST_BACKOFF_BASE_SECONDS,
        backoff_max: float = POST_BACKOFF_MAX_SECONDS,
        post_func=post_event,
    ):
        self.args = args
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(0.0, float(backoff_max))
        self.post_func = post_func
        self.queue = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._sentinel = object()
        self._thread = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="veil-live-event-poster", daemon=True)
        self._thread.start()

    def enqueue(self, event: dict, source: str = "event") -> bool:
        try:
            self.queue.put_nowait({"event": event, "attempt": 1, "source": source})
            return True
        except queue.Full:
            print(
                f"post queue full; dropping {source} event "
                f"{event.get('kind')} for {event.get('device_id')}",
                file=sys.stderr,
                flush=True,
            )
            return False

    def stop(self, drain_seconds: float = 5.0) -> None:
        deadline = time.time() + max(0.0, drain_seconds)
        while self._thread and self._thread.is_alive() and time.time() < deadline:
            if self.queue.unfinished_tasks == 0:
                break
            time.sleep(0.05)
        if self.queue.unfinished_tasks:
            print(
                f"post queue shutdown with {self.queue.unfinished_tasks} event(s) still pending",
                file=sys.stderr,
                flush=True,
            )
        self._stop.set()
        if self._thread and self._thread.is_alive():
            try:
                self.queue.put(self._sentinel, timeout=1)
            except queue.Full:
                pass
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                print("post worker did not stop before shutdown timeout", file=sys.stderr, flush=True)

    def _retry_delay(self, attempt: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** max(0, attempt - 1)))

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            try:
                if item is self._sentinel:
                    return
                self._post_or_retry(item)
            finally:
                self.queue.task_done()

    def _post_or_retry(self, item: dict) -> None:
        event = item["event"]
        attempt = item["attempt"]
        source = item.get("source") or "event"
        try:
            self.post_func(self.args, event)
            print(json.dumps({"posted": event.get("kind"), "device_id": event.get("device_id")}), flush=True)
        except Exception as exc:
            if self._stop.is_set() or attempt >= self.max_attempts:
                print(
                    f"post failed after {attempt} attempt(s); dropping {source} event "
                    f"{event.get('kind')} for {event.get('device_id')}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return
            delay = self._retry_delay(attempt)
            print(
                f"post failed for {source} event {event.get('kind')} for {event.get('device_id')}: "
                f"{exc}; retry {attempt + 1}/{self.max_attempts} in {delay:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            if self._stop.wait(delay):
                return
            item["attempt"] = attempt + 1
            try:
                self.queue.put_nowait(item)
            except queue.Full:
                print(
                    f"post queue full; dropping retry for {source} event "
                    f"{event.get('kind')} for {event.get('device_id')}",
                    file=sys.stderr,
                    flush=True,
                )


def node_user(node: dict) -> dict:
    user = node.get("user")
    return user if isinstance(user, dict) else {}


def label_from_node(node: dict, fallback: str) -> str:
    user = node_user(node)
    return (
        user.get("longName")
        or user.get("long_name")
        or user.get("shortName")
        or user.get("short_name")
        or user.get("id")
        or fallback
    )


def remember_label(known_labels: dict | None, device_id: str, label: str | None) -> None:
    if known_labels is not None and label and label != device_id:
        known_labels[device_id] = label


def label_for_device(known_labels: dict | None, device_id: str, fallback: str | None = None) -> str:
    return (known_labels or {}).get(device_id) or fallback or device_id


def position_from_node(node: dict) -> dict | None:
    pos = node.get("position")
    if not isinstance(pos, dict):
        return None
    lat = first_coord(pos, "latitude", "latitude_i")
    if lat is None:
        lat = first_coord(pos, "latitude", "latitudeI")
    lon = first_coord(pos, "longitude", "longitude_i")
    if lon is None:
        lon = first_coord(pos, "longitude", "longitudeI")
    if lat is None or lon is None:
        return None
    return {
        "lat": lat,
        "lon": lon,
        "alt_m": as_float(pos.get("altitude")),
        "accuracy_m": None,
    }


def event_from_node_info(node: dict, args) -> dict | None:
    user = node_user(node)
    device_id = node_id(node)
    if not device_id or device_id == "unknown":
        return None
    if args.gateway_node_id and device_id == args.gateway_node_id:
        return None
    last_heard = node.get("lastHeard") or node.get("last_heard")
    observed_at = utcnow()
    if isinstance(last_heard, (int, float)) and last_heard > 0:
        observed_at = datetime.fromtimestamp(last_heard, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event = {
        "schema": "veil.live.v1",
        "kind": "status",
        "device_id": device_id,
        "label": label_from_node(node, device_id),
        "observed_at": observed_at,
        "link": {
            "gateway_id": args.gateway_id,
            "gateway_node_id": args.gateway_node_id,
            "snr_db": as_float(node.get("snr")),
            "rssi_dbm": None,
            "hops": node.get("hopsAway") or node.get("hops_away"),
        },
        "source": {
            "protocol": "meshtastic",
            "transport": "lora",
            "ingress_transport": args.transport,
            "gateway": args.gateway_name,
        },
        "metadata": {
            "source": "meshtastic_nodedb",
            "gateway_name": args.gateway_name,
            "gateway_node_id": args.gateway_node_id,
            "node": json_safe(node),
        },
        "data": {"portnum": "NODEDB", "node": json_safe(node)},
    }
    position = position_from_node(node)
    if position:
        event["position"] = position
        event["metadata"]["position"] = json_safe(node.get("position"))
    return event


def publish_known_nodes(args, iface, seen: dict, known_labels: dict | None = None, poster: EventPoster | None = None) -> int:
    nodes = getattr(iface, "nodesByNum", None)
    if not nodes:
        return 0
    count = 0
    failed = 0
    for node in list(nodes.values()):
        event = event_from_node_info(node, args)
        if not event:
            continue
        remember_label(known_labels, event["device_id"], event.get("label"))
        signature = json.dumps({
            "label": event.get("label"),
            "observed_at": event.get("observed_at"),
            "position": event.get("position"),
            "snr": event.get("link", {}).get("snr_db"),
            "hops": event.get("link", {}).get("hops"),
        }, sort_keys=True)
        if seen.get(event["device_id"]) == signature:
            continue
        try:
            if poster:
                posted = poster.enqueue(event, source="nodedb")
            else:
                post_event(args, event)
                posted = True
            if posted:
                seen[event["device_id"]] = signature
                count += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            print(
                f"nodedb post failed for {event.get('device_id')}: {exc}",
                file=sys.stderr,
                flush=True,
            )
    if count or failed:
        status = {"posted": "nodedb", "count": count}
        if poster:
            status["queued"] = True
        if failed:
            status["failed"] = failed
        print(json.dumps(status), flush=True)
    return count


def position_details(decoded: dict) -> dict | None:
    pos = decoded.get("position") or decoded.get("POSITION_APP")
    pos = message_to_dict(pos)
    if not isinstance(pos, dict) and portnum_name(decoded) == "POSITION_APP":
        pos = parse_message(mesh_pb2.Position, decoded_payload(decoded))
    if not isinstance(pos, dict):
        return None
    return pos


def position_from_decoded(decoded: dict) -> dict | None:
    pos = position_details(decoded)
    if not isinstance(pos, dict):
        return None
    lat = first_coord(pos, "latitude", "latitude_i")
    if lat is None:
        lat = first_coord(pos, "latitude", "latitudeI")
    lon = first_coord(pos, "longitude", "longitude_i")
    if lon is None:
        lon = first_coord(pos, "longitude", "longitudeI")
    if lat is None or lon is None:
        return None
    return {
        "lat": lat,
        "lon": lon,
        "alt_m": as_float(pos.get("altitude")),
        "accuracy_m": as_float(pos.get("gps_accuracy")),
    }


def text_from_decoded(decoded: dict) -> str | None:
    port = portnum_name(decoded)
    for key in ("text", "message"):
        value = decoded.get(key)
        if isinstance(value, str):
            return value
    if port == "TEXT_MESSAGE_APP":
        payload = decoded_payload(decoded)
        if payload:
            try:
                return payload.decode("utf-8", "replace")
            except Exception:
                return None
    return None


def user_from_decoded(decoded: dict) -> dict | None:
    user = decoded.get("user") or decoded.get("NODEINFO_APP")
    user = message_to_dict(user)
    if not isinstance(user, dict) and portnum_name(decoded) == "NODEINFO_APP":
        user = parse_message(mesh_pb2.User, decoded_payload(decoded))
    return user if isinstance(user, dict) else None


def first_present(mapping: dict, *keys):
    for key in keys:
        if isinstance(mapping, dict) and key in mapping:
            return mapping.get(key)
    return None


def finite_number(value):
    number = as_float(value)
    if number is None or not math.isfinite(number):
        return None
    return number


def packet_hops_traveled(packet: dict):
    hop_start = finite_number(first_present(packet, "hopStart", "hop_start"))
    hop_limit = finite_number(first_present(packet, "hopLimit", "hop_limit"))
    if hop_start is None or hop_limit is None:
        return None
    hops = max(0, hop_start - hop_limit)
    return int(hops) if hops.is_integer() else hops


def telemetry_details(decoded: dict) -> dict | None:
    if portnum_name(decoded) != "TELEMETRY_APP":
        return None
    telemetry = (
        decoded.get("telemetry")
        or decoded.get("TELEMETRY_APP")
        or decoded.get("device_metrics")
        or decoded.get("deviceMetrics")
    )
    telemetry = message_to_dict(telemetry)
    if isinstance(telemetry, dict) and ("device_metrics" in telemetry or "deviceMetrics" in telemetry):
        return telemetry
    if isinstance(telemetry, dict) and any(k in telemetry for k in ("battery_level", "batteryLevel", "voltage")):
        return {"device_metrics": telemetry}
    parsed = parse_message(telemetry_pb2.Telemetry, decoded_payload(decoded))
    return parsed if isinstance(parsed, dict) else None


def iso_from_epoch_seconds(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def telemetry_battery_summary(telemetry: dict) -> dict:
    device = first_present(telemetry, "device_metrics", "deviceMetrics") or {}
    env = first_present(telemetry, "environment_metrics", "environmentMetrics") or {}
    power = first_present(telemetry, "power_metrics", "powerMetrics") or {}
    summary = {}
    battery_level = as_float(first_present(device, "battery_level", "batteryLevel"))
    voltage = as_float(first_present(device, "voltage"))
    env_voltage = as_float(first_present(env, "voltage"))
    env_current = as_float(first_present(env, "current"))
    if battery_level is not None:
        summary["battery_level_pct"] = battery_level
    if voltage is not None:
        summary["voltage_v"] = voltage
    if env_voltage is not None:
        summary["environment_voltage_v"] = env_voltage
    if env_current is not None:
        summary["environment_current_a"] = env_current
    channels = []
    for idx in range(1, 9):
        ch_voltage = as_float(first_present(power, f"ch{idx}_voltage", f"ch{idx}Voltage"))
        ch_current = as_float(first_present(power, f"ch{idx}_current", f"ch{idx}Current"))
        if ch_voltage is not None or ch_current is not None:
            channels.append({
                "channel": idx,
                "voltage_v": ch_voltage,
                "current_a": ch_current,
            })
    if channels:
        summary["power_channels"] = channels
    for out_key, source_key, camel_key in (
        ("uptime_seconds", "uptime_seconds", "uptimeSeconds"),
        ("channel_utilization_pct", "channel_utilization", "channelUtilization"),
        ("air_util_tx_pct", "air_util_tx", "airUtilTx"),
    ):
        value = as_float(first_present(device, source_key, camel_key))
        if value is not None:
            summary[out_key] = value
    return summary


def packet_metadata(packet: dict, decoded: dict, args) -> dict:
    keys = (
        "id", "from", "fromId", "to", "toId", "rxTime", "rxSnr", "rxRssi",
        "hopLimit", "hopStart", "channel", "priority", "wantAck", "viaMqtt",
        "pkiEncrypted", "nextHop", "relayNode",
    )
    packet_view = {k: json_safe(packet.get(k)) for k in keys if packet.get(k) is not None}
    decoded_view = {k: json_safe(v) for k, v in decoded.items() if k != "payload"}
    payload = decoded_payload(decoded)
    if payload is not None:
        decoded_view["payload_bytes"] = len(payload)
    return {
        "packet": packet_view,
        "decoded": decoded_view,
        "portnum": portnum_name(decoded),
        "gateway_name": args.gateway_name,
        "gateway_node_id": args.gateway_node_id,
        "ingress_transport": args.transport,
    }


def event_from_packet(packet: dict, args, known_labels: dict | None = None) -> dict | None:
    decoded = packet.get("decoded") or {}
    sender = node_id(packet.get("fromId"), packet.get("from"))
    if args.gateway_node_id and sender == args.gateway_node_id:
        return None
    gateway = node_id(args.gateway_id) if args.gateway_id else node_id(packet.get("toId"), packet.get("to"))
    observed = packet.get("rxTime")
    observed_at = utcnow()
    if isinstance(observed, (int, float)) and observed > 0:
        observed_at = datetime.fromtimestamp(observed, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    base = {
        "schema": "veil.live.v1",
        "device_id": sender,
        "label": label_for_device(known_labels, sender),
        "observed_at": observed_at,
        "link": {
            "gateway_id": gateway,
            "snr_db": as_float(packet.get("rxSnr")),
            "rssi_dbm": as_float(packet.get("rxRssi")),
            "hops": packet_hops_traveled(packet),
        },
        "source": {
            "protocol": "meshtastic",
            "transport": "lora",
            "ingress_transport": args.transport,
            "gateway": args.gateway_name,
        },
        "metadata": packet_metadata(packet, decoded, args),
    }
    if args.gateway_node_id:
        base["link"]["gateway_node_id"] = args.gateway_node_id
    pos = position_from_decoded(decoded)
    if pos:
        pos_meta = position_details(decoded)
        event = {**base, "kind": "position", "position": pos}
        if pos_meta:
            event["metadata"]["position"] = pos_meta
        pos_decoded = decoded.get("position") if isinstance(decoded.get("position"), dict) else {}
        if not pos_decoded and isinstance(pos_meta, dict):
            pos_decoded = pos_meta
        speed = as_float(pos_decoded.get("ground_speed") or pos_decoded.get("groundSpeed"))
        heading = normalize_heading(pos_decoded.get("ground_track") or pos_decoded.get("groundTrack"))
        if speed is not None or heading is not None:
            event["motion"] = {"speed_mps": speed, "heading_deg": heading}
        return event
    user = user_from_decoded(decoded)
    if user:
        label = user.get("long_name") or user.get("longName") or user.get("id") or sender
        remember_label(known_labels, sender, label)
        return {
            **base,
            "kind": "status",
            "label": label,
            "data": {"portnum": portnum_name(decoded), "user": user},
        }
    text = text_from_decoded(decoded)
    if text:
        return {**base, "kind": "message", "message": text}
    telemetry = telemetry_details(decoded)
    if telemetry:
        data = {
            "portnum": "TELEMETRY_APP",
            "telemetry": json_safe(telemetry),
        }
        battery = telemetry_battery_summary(telemetry)
        if battery:
            data["battery"] = battery
        telemetry_at = iso_from_epoch_seconds(first_present(telemetry, "time"))
        if telemetry_at:
            data["telemetry_observed_at"] = telemetry_at
        return {**base, "kind": "status", "data": data}
    if decoded and portnum_name(decoded) not in ("ROUTING_APP", "ADMIN_APP"):
        safe_decoded = {k: json_safe(v) for k, v in decoded.items() if k != "payload"}
        return {**base, "kind": "data", "data": {"portnum": portnum_name(decoded), "decoded": safe_decoded}}
    return None


def enqueue_packet_event(packet: dict, args, known_labels: dict | None, poster: EventPoster) -> bool:
    event = event_from_packet(packet, args, known_labels)
    if not event:
        return False
    return poster.enqueue(event, source="packet")


def _lookup_node_by_num(nodes, num):
    if nodes is None or num is None:
        return None
    for key in (num, str(num), f"!{num:08x}"):
        try:
            node = nodes.get(key) if hasattr(nodes, "get") else None
        except Exception:
            node = None
        if node:
            return node
    return None


def _iter_nodes(nodes):
    if nodes is None:
        return
    values = nodes.values() if hasattr(nodes, "values") else nodes
    try:
        iterator = iter(values)
    except TypeError:
        return
    for node in iterator:
        yield node


def _node_marks_self(node) -> bool:
    return any(bool(_get_value(node, key)) for key in ("is_self", "isSelf", "self", "isMine", "is_mine"))


def _node_id_from_info(info):
    detected = node_id(info)
    return detected if detected != "unknown" else None


def detect_gateway_node_id(iface, explicit=None, address=None):
    if explicit:
        return node_id(explicit)
    for getter in ("getMyNodeInfo", "getMyUser"):
        try:
            info = getattr(iface, getter)()
        except Exception:
            continue
        detected = _node_id_from_info(info)
        if detected:
            return detected
    local_num = None
    for attr in ("myInfo", "myinfo", "myNodeInfo", "my_node_info", "localNode", "local_node"):
        info = getattr(iface, attr, None)
        detected = _node_id_from_info(info)
        if detected:
            return detected
        for key in ("my_node_num", "myNodeNum", "node_num", "nodeNum", "num"):
            value = _get_value(info, key)
            num = _node_num(value)
            if num is not None:
                local_num = num
                break
        if local_num is not None:
            break
    for nodes_attr in ("nodesByNum", "nodes", "nodeDB", "nodeDb", "nodedb"):
        nodes = getattr(iface, nodes_attr, None)
        node = _lookup_node_by_num(nodes, local_num)
        detected = _node_id_from_info(node)
        if detected:
            return detected
        for candidate in _iter_nodes(nodes):
            if _node_marks_self(candidate):
                detected = _node_id_from_info(candidate)
                if detected:
                    return detected
    if address:
        return None
    return None


def close_interface(iface) -> None:
    if not iface:
        return
    try:
        setattr(iface, "_veil_expected_close", True)
    except Exception:
        pass
    try:
        iface.close()
    except Exception:
        pass


def notify_interface_closed(iface, on_disconnect, reason: str) -> None:
    if getattr(iface, "_veil_expected_close", False):
        return
    if on_disconnect:
        on_disconnect(reason, iface)


def wrap_interface_close(iface, on_disconnect):
    if not iface or getattr(iface, "_veil_close_wrapped", False):
        return iface
    original_close = iface.close

    def close_with_disconnect(*args, **kwargs):
        if getattr(iface, "_veil_closing", False):
            return None
        notify_interface_closed(iface, on_disconnect, "interface closed")
        iface._veil_closing = True
        try:
            return original_close(*args, **kwargs)
        finally:
            iface._veil_closing = False

    iface.close = close_with_disconnect
    iface._veil_close_wrapped = True
    return iface


def connect_interface(args, on_disconnect=None):
    if args.transport == "serial":
        from meshtastic.serial_interface import SerialInterface

        return wrap_interface_close(SerialInterface(devPath=args.port), on_disconnect)
    if args.transport in ("tcp", "internet"):
        from meshtastic.tcp_interface import TCPInterface

        return wrap_interface_close(TCPInterface(hostname=args.host), on_disconnect)
    if args.transport == "bluetooth":
        from meshtastic.ble_interface import BLEClient, BLEInterface

        BLEInterface._veil_on_disconnect = on_disconnect
        if not getattr(BLEInterface, "_veil_bluez_patch", False):
            original_connect = BLEInterface.connect

            def connect_with_bluez_known_device(self, address=None):
                try:
                    return original_connect(self, address)
                except BLEInterface.BLEError as exc:
                    if not address or exc.kind != BLEInterface.BLEError.DEVICE_NOT_FOUND:
                        raise
                    print(
                        f"BLE scan did not return {address}; trying direct BlueZ connection",
                        file=sys.stderr,
                        flush=True,
                    )
                    from bleak.backends.device import BLEDevice

                    bluez_path = "/org/bluez/hci0/dev_" + address.replace(":", "_").upper()
                    device = BLEDevice(address, address, {"path": bluez_path, "props": {}})

                    def disconnected(_client):
                        notify_interface_closed(
                            self,
                            getattr(BLEInterface, "_veil_on_disconnect", None),
                            "BLE disconnected",
                        )
                        self.close()

                    client = BLEClient(device, disconnected_callback=disconnected, timeout=20)
                    try:
                        client.async_await(client.bleak_client.connect(), timeout=25)
                        print(f"direct BlueZ BLE connect completed for {address}", flush=True)
                    except concurrent.futures.TimeoutError as timeout_exc:
                        try:
                            client.disconnect()
                        except Exception:
                            pass
                        raise BLEInterface.BLEError(
                            f"Timed out connecting to known BlueZ BLE device {address}",
                            BLEInterface.BLEError.DEVICE_NOT_FOUND,
                        ) from timeout_exc
                    return client

            BLEInterface.connect = connect_with_bluez_known_device
            BLEInterface._veil_bluez_patch = True

        return wrap_interface_close(BLEInterface(args.address if args.address else None), on_disconnect)
    raise ValueError(f"unsupported transport: {args.transport}")


class CommandFileReader:
    def __init__(self, path: str):
        self.path = path
        self.fh = None
        self._open(seek_end=True)

    def _open(self, seek_end: bool = False) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        open(self.path, "a", encoding="utf-8").close()
        self.close()
        self.fh = open(self.path, "r", encoding="utf-8")
        if seek_end:
            self.fh.seek(0, os.SEEK_END)

    def _needs_reopen(self) -> bool:
        if not self.fh:
            return True
        try:
            current = os.stat(self.path)
            opened = os.fstat(self.fh.fileno())
        except OSError:
            return False
        return (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or current.st_size < self.fh.tell()

    def readline(self):
        if not self.fh:
            return ""
        line = self.fh.readline()
        if line:
            return line
        if self._needs_reopen():
            self._open(seek_end=False)
            return self.fh.readline()
        return ""

    def close(self) -> None:
        if self.fh:
            try:
                self.fh.close()
            finally:
                self.fh = None


def command_file_reader(path: str | None):
    if not path:
        return None
    return CommandFileReader(path)


def handle_command(iface, cmd: dict) -> str:
    command = str(cmd.get("command") or "request_position")
    device_id = node_id(cmd.get("device_id"))
    channel_index = int(cmd.get("channel_index") or 0)
    hop_limit = cmd.get("hop_limit")
    if hop_limit is not None:
        hop_limit = int(hop_limit)
    if command == "request_position":
        # MeshInterface.sendPosition(..., wantResponse=True) blocks in
        # waitForPosition(). The bridge should keep listening and let the
        # normal receive handler post any returned POSITION_APP packet.
        iface.sendData(
            mesh_pb2.Position(),
            device_id,
            portNum=portnums_pb2.PortNum.POSITION_APP,
            wantResponse=True,
            channelIndex=channel_index,
            hopLimit=hop_limit,
        )
        return f"queued position request for {device_id}"
    if command == "traceroute":
        iface.sendTraceRoute(dest=device_id, hopLimit=hop_limit or 3, channelIndex=channel_index)
        return f"sent traceroute to {device_id}"
    raise ValueError(f"unsupported command: {command}")


def poll_commands(reader, iface) -> None:
    if not reader:
        return
    while True:
        line = reader.readline()
        if not line:
            break
        try:
            cmd = json.loads(line)
            result = handle_command(iface, cmd)
            print(json.dumps({"command": cmd.get("command"), "device_id": cmd.get("device_id"), "status": result}), flush=True)
        except Exception as exc:
            print(f"command failed: {exc}", file=sys.stderr, flush=True)


def interface_disconnect_reason(iface) -> str | None:
    if not iface:
        return "not connected"
    connected = getattr(iface, "isConnected", None)
    if hasattr(connected, "is_set") and not connected.is_set():
        return "interface marked disconnected"
    if connected is False:
        return "interface marked disconnected"
    return None


def probe_interface(iface) -> str | None:
    reason = interface_disconnect_reason(iface)
    if reason:
        return reason
    send_heartbeat = getattr(iface, "sendHeartbeat", None)
    if callable(send_heartbeat):
        try:
            send_heartbeat()
        except Exception as exc:
            return f"heartbeat failed: {exc}"
    return None


def reconnect_delay(attempt: int) -> float:
    return min(RECONNECT_MAX_SECONDS, RECONNECT_BASE_SECONDS * (2 ** min(max(attempt - 1, 0), 4)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--veil", default=os.environ.get("VEIL_URL", "http://127.0.0.1:4173"))
    parser.add_argument("--token", default=os.environ.get("VEIL_LIVE_TOKEN"))
    parser.add_argument("--transport", choices=["serial", "bluetooth", "tcp", "internet"], default="serial")
    parser.add_argument("--port", help="Serial device path, for example /dev/ttyUSB0")
    parser.add_argument("--address", help="Bluetooth MAC/address or Meshtastic BLE device identifier")
    parser.add_argument("--host", help="Meshtastic TCP host")
    parser.add_argument("--gateway-id", help="Stable gateway node id to stamp on incoming packets")
    parser.add_argument("--gateway-node-id", help="Meshtastic node id of the local gateway radio; packets from this node are ignored as tracked devices")
    parser.add_argument("--gateway-name", default="Meshtastic gateway")
    parser.add_argument("--command-file", help="JSONL command queue file managed by server.js")
    parser.add_argument("--register", action="store_true", help="Register the gateway in VEIL before listening")
    args = parser.parse_args()

    if args.register:
        headers = {"Content-Type": "application/json"}
        if args.token:
            headers["X-VEIL-Live-Token"] = args.token
        requests.post(
            args.veil.rstrip("/") + "/api/live/gateways",
            headers=headers,
            json={
                "gateway_id": args.gateway_id or args.address or args.port or args.host or args.gateway_name,
                "name": args.gateway_name,
                "protocol": "meshtastic",
                "transport": args.transport,
                "address": args.address or args.port or args.host or "",
                "connect": False,
            },
            timeout=10,
        ).raise_for_status()

    poster = EventPoster(args)
    poster.start()
    atexit.register(poster.stop)
    command_reader = command_file_reader(args.command_file)
    stopped = False
    iface = None
    seen_nodes = {}
    known_labels = {}
    configured_gateway_node_id = args.gateway_node_id
    last_node_sync = 0.0
    last_health_check = 0.0
    reconnect_reason = None
    reconnect_attempt = 0

    def stop(_signum=None, _frame=None):
        nonlocal stopped
        stopped = True
        close_interface(iface)

    def request_reconnect(reason: str, interface=None) -> None:
        nonlocal reconnect_reason
        if stopped:
            return
        if interface is not None and iface is not None and interface is not iface:
            return
        if not reconnect_reason:
            reconnect_reason = reason
            print(f"Meshtastic link lost: {reason}", file=sys.stderr, flush=True)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def on_receive(packet, interface=None):
        if interface is not None and iface is not None and interface is not iface:
            return
        enqueue_packet_event(packet, args, known_labels, poster)

    def on_connection_lost(interface=None):
        request_reconnect("connection lost", interface)

    def connect_current() -> None:
        nonlocal iface, last_health_check, last_node_sync, reconnect_reason
        iface = connect_interface(args, request_reconnect)
        args.gateway_node_id = detect_gateway_node_id(iface, configured_gateway_node_id, args.address)
        if args.gateway_node_id:
            print(f"gateway radio node: {args.gateway_node_id}", flush=True)
        last_health_check = time.time()
        last_node_sync = 0.0
        reconnect_reason = None
        print(f"listening to Meshtastic {args.transport}; posting to {args.veil}", flush=True)

    def reconnect(reason: str) -> None:
        nonlocal iface, reconnect_attempt, reconnect_reason
        close_interface(iface)
        iface = None
        while not stopped:
            reconnect_attempt += 1
            delay = reconnect_delay(reconnect_attempt)
            print(
                f"reconnecting to Meshtastic {args.transport} after {reason}; "
                f"retry {reconnect_attempt} in {delay:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            deadline = time.time() + delay
            while not stopped and time.time() < deadline:
                time.sleep(0.25)
            if stopped:
                return
            try:
                connect_current()
                reconnect_attempt = 0
                return
            except Exception as exc:
                reason = str(exc)
                reconnect_reason = reason

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")
    try:
        connect_current()
    except Exception as exc:
        reconnect_reason = str(exc)
    while not stopped:
        if reconnect_reason:
            reconnect(reconnect_reason)
            continue
        poll_commands(command_reader, iface)
        now = time.time()
        if now - last_health_check >= HEALTH_CHECK_SECONDS:
            reason = probe_interface(iface)
            if reason:
                request_reconnect(reason, iface)
                continue
            last_health_check = now
        if now - last_node_sync >= 10:
            try:
                publish_known_nodes(args, iface, seen_nodes, known_labels, poster)
            except Exception as exc:
                print(f"nodedb sync failed: {exc}", file=sys.stderr, flush=True)
            last_node_sync = now
        time.sleep(1)
    if command_reader:
        command_reader.close()
    close_interface(iface)
    poster.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
