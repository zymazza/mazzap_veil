#!/usr/bin/env python3
"""Regression tests for the Meshtastic live bridge post queue."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import time
import types
import unittest
from types import SimpleNamespace


ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "scripts" / "live" / "meshtastic_serial_bridge.py"


def install_import_stubs() -> None:
    class StubMessage:
        pass

    class StubProto:
        def ParseFromString(self, _payload):
            return None

    json_format = types.ModuleType("google.protobuf.json_format")
    json_format.MessageToDict = lambda value, preserving_proto_field_name=True: value
    message = types.ModuleType("google.protobuf.message")
    message.Message = StubMessage
    protobuf = types.ModuleType("google.protobuf")
    protobuf.json_format = json_format
    protobuf.message = message
    google = types.ModuleType("google")
    google.protobuf = protobuf

    mesh_pb2 = types.ModuleType("meshtastic.protobuf.mesh_pb2")
    mesh_pb2.Position = StubProto
    mesh_pb2.User = StubProto
    telemetry_pb2 = types.ModuleType("meshtastic.protobuf.telemetry_pb2")
    telemetry_pb2.Telemetry = StubProto
    portnums_pb2 = types.ModuleType("meshtastic.protobuf.portnums_pb2")
    portnums_pb2.PortNum = SimpleNamespace(POSITION_APP=1, Name=lambda value: str(value))
    meshtastic_protobuf = types.ModuleType("meshtastic.protobuf")
    meshtastic_protobuf.mesh_pb2 = mesh_pb2
    meshtastic_protobuf.portnums_pb2 = portnums_pb2
    meshtastic_protobuf.telemetry_pb2 = telemetry_pb2
    meshtastic = types.ModuleType("meshtastic")
    meshtastic.protobuf = meshtastic_protobuf

    pubsub = types.ModuleType("pubsub")
    pubsub.pub = SimpleNamespace(subscribe=lambda *args, **kwargs: None)

    sys.modules.setdefault("google", google)
    sys.modules.setdefault("google.protobuf", protobuf)
    sys.modules.setdefault("google.protobuf.json_format", json_format)
    sys.modules.setdefault("google.protobuf.message", message)
    sys.modules.setdefault("meshtastic", meshtastic)
    sys.modules.setdefault("meshtastic.protobuf", meshtastic_protobuf)
    sys.modules.setdefault("meshtastic.protobuf.mesh_pb2", mesh_pb2)
    sys.modules.setdefault("meshtastic.protobuf.portnums_pb2", portnums_pb2)
    sys.modules.setdefault("meshtastic.protobuf.telemetry_pb2", telemetry_pb2)
    sys.modules.setdefault("pubsub", pubsub)


def load_bridge():
    install_import_stubs()
    spec = importlib.util.spec_from_file_location("meshtastic_serial_bridge_under_test", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MeshtasticBridgeQueueTest(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()
        self.args = SimpleNamespace(
            veil="http://127.0.0.1:4173",
            token=None,
            gateway_node_id=None,
            gateway_id="!feed0001",
            gateway_name="test gateway",
            transport="serial",
        )

    def test_receive_path_only_enqueues_without_posting(self):
        posts = []

        def slow_post(_args, event):
            posts.append(event)
            time.sleep(1)

        poster = self.bridge.EventPoster(self.args, maxsize=10, post_func=slow_post)
        packet = {
            "from": "!00000001",
            "to": "!feed0001",
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }

        started = time.perf_counter()
        enqueued = self.bridge.enqueue_packet_event(packet, self.args, {}, poster)
        elapsed = time.perf_counter() - started

        self.assertTrue(enqueued)
        self.assertLess(elapsed, 0.05)
        self.assertEqual(posts, [])
        self.assertEqual(poster.queue.qsize(), 1)

    def test_worker_retries_transient_post_failure(self):
        attempts = []

        def flaky_post(_args, event):
            attempts.append(event["device_id"])
            if len(attempts) < 3:
                raise RuntimeError("temporary outage")

        poster = self.bridge.EventPoster(
            self.args,
            maxsize=10,
            max_attempts=3,
            backoff_base=0.001,
            backoff_max=0.001,
            post_func=flaky_post,
        )
        poster.start()
        try:
            self.assertTrue(poster.enqueue({"kind": "message", "device_id": "!00000001"}, source="packet"))
            poster.queue.join()
        finally:
            poster.stop(drain_seconds=0)

        self.assertEqual(attempts, ["!00000001", "!00000001", "!00000001"])

    def test_known_node_publish_continues_after_one_post_failure(self):
        iface = SimpleNamespace(
            nodesByNum={
                1: {"user": {"id": "!00000001", "longName": "first"}, "lastHeard": 100},
                2: {"user": {"id": "!00000002", "longName": "second"}, "lastHeard": 101},
            }
        )
        seen = {}
        posted = []
        original_post_event = self.bridge.post_event

        def flaky_post(_args, event):
            posted.append(event["device_id"])
            if len(posted) == 1:
                raise RuntimeError("temporary outage")

        self.bridge.post_event = flaky_post
        try:
            count = self.bridge.publish_known_nodes(self.args, iface, seen, {})
        finally:
            self.bridge.post_event = original_post_event

        self.assertEqual(posted, ["!00000001", "!00000002"])
        self.assertEqual(count, 1)
        self.assertNotIn("!00000001", seen)
        self.assertIn("!00000002", seen)

    def test_packet_hops_use_start_minus_remaining_limit(self):
        packet = {
            "from": "!00000001",
            "to": "!feed0001",
            "hopStart": 5,
            "hopLimit": 3,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }

        event = self.bridge.event_from_packet(packet, self.args, {})

        self.assertIsNotNone(event)
        self.assertEqual(event["link"]["hops"], 2)

    def test_packet_hop_limit_alone_does_not_create_hops(self):
        packet = {
            "from": "!00000001",
            "to": "!feed0001",
            "hopLimit": 3,
            "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
        }

        event = self.bridge.event_from_packet(packet, self.args, {})

        self.assertIsNotNone(event)
        self.assertIsNone(event["link"]["hops"])

    def test_heading_normalization_is_explicit_and_conservative(self):
        self.assertEqual(self.bridge.normalize_heading(0), 0)
        self.assertEqual(self.bridge.normalize_heading(90.5), 90.5)
        self.assertEqual(self.bridge.normalize_heading(360), 360)
        self.assertEqual(self.bridge.normalize_heading(9_000_000), 90)
        self.assertEqual(self.bridge.normalize_heading(36_000_000), 360)
        self.assertIsNone(self.bridge.normalize_heading(361))
        self.assertIsNone(self.bridge.normalize_heading(900))
        self.assertIsNone(self.bridge.normalize_heading(3600))
        self.assertIsNone(self.bridge.normalize_heading(36_000_001))
        self.assertIsNone(self.bridge.normalize_heading(-1))

    def test_node_id_canonicalizes_common_meshtastic_representations(self):
        self.assertEqual(self.bridge.node_id(0xABCD1234), "!abcd1234")
        self.assertEqual(self.bridge.node_id("2882343476"), "!abcd1234")
        self.assertEqual(self.bridge.node_id("!ABCD1234"), "!abcd1234")
        self.assertEqual(self.bridge.node_id("0xABCD1234"), "!abcd1234")
        self.assertEqual(self.bridge.node_id("abcd1234"), "!abcd1234")
        self.assertEqual(
            self.bridge.node_id({"num": 0xABCD1234, "user": {"id": "!badc0ffe"}}),
            "!abcd1234",
        )
        self.assertEqual(self.bridge.node_id("gateway-a"), "gateway-a")

    def test_packet_and_nodedb_events_share_canonical_device_id(self):
        nodedb_event = self.bridge.event_from_node_info(
            {"num": 0xABCD1234, "user": {"id": "!ABCD1234", "longName": "node"}},
            self.args,
        )
        packet_event = self.bridge.event_from_packet(
            {
                "fromId": "!ABCD1234",
                "from": 0xABCD1234,
                "to": "!feed0001",
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
            },
            self.args,
            {},
        )

        self.assertIsNotNone(nodedb_event)
        self.assertIsNotNone(packet_event)
        self.assertEqual(nodedb_event["device_id"], "!abcd1234")
        self.assertEqual(packet_event["device_id"], "!abcd1234")

    def test_packet_event_preserves_configured_gateway_id(self):
        args = SimpleNamespace(**{**self.args.__dict__, "gateway_id": "gateway-a"})
        event = self.bridge.event_from_packet(
            {
                "from": 0xABCD1234,
                "to": 0xFEED0001,
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
            },
            args,
            {},
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["device_id"], "!abcd1234")
        self.assertEqual(event["link"]["gateway_id"], "gateway-a")

    def test_gateway_node_detection_uses_real_node_metadata_not_ble_address(self):
        self.assertEqual(
            self.bridge.detect_gateway_node_id(SimpleNamespace(), explicit="!ABCD1234", address="AA:BB:CC:DD:EE:FF"),
            "!abcd1234",
        )
        self.assertEqual(
            self.bridge.detect_gateway_node_id(
                SimpleNamespace(getMyNodeInfo=lambda: {"num": 0xABCD1234}),
                address="AA:BB:CC:DD:EE:FF",
            ),
            "!abcd1234",
        )
        self.assertEqual(
            self.bridge.detect_gateway_node_id(
                SimpleNamespace(myInfo=SimpleNamespace(my_node_num=0xABCD1234)),
                address="AA:BB:CC:DD:EE:FF",
            ),
            "!abcd1234",
        )
        self.assertEqual(
            self.bridge.detect_gateway_node_id(
                SimpleNamespace(nodesByNum={0xABCD1234: {"num": 0xABCD1234, "isSelf": True}}),
                address="AA:BB:CC:DD:EE:FF",
            ),
            "!abcd1234",
        )
        self.assertIsNone(
            self.bridge.detect_gateway_node_id(SimpleNamespace(), address="AA:BB:CC:DD:EE:FF")
        )

    def test_command_reader_reopens_after_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            command_file = pathlib.Path(tmp) / "gateway.jsonl"
            command_file.write_text("", encoding="utf-8")
            reader = self.bridge.command_file_reader(str(command_file))
            try:
                command_file.write_text('{"id":"old"}\n', encoding="utf-8")
                self.assertEqual(reader.readline(), '{"id":"old"}\n')
                self.assertEqual(reader.readline(), "")

                command_file.rename(pathlib.Path(tmp) / "gateway.jsonl.1")
                command_file.write_text('{"id":"new"}\n', encoding="utf-8")

                self.assertEqual(reader.readline(), '{"id":"new"}\n')
            finally:
                reader.close()


if __name__ == "__main__":
    unittest.main()
