#!/usr/bin/env python3
"""Regression tests for live replay URL construction."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPLAY_DEMO_PATH = ROOT / "scripts" / "live" / "replay_demo.py"


def load_replay_demo():
    requests = types.ModuleType("requests")
    requests.post = lambda *args, **kwargs: None
    sys.modules.setdefault("requests", requests)

    spec = importlib.util.spec_from_file_location("replay_demo_under_test", REPLAY_DEMO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReplayDemoUrlTest(unittest.TestCase):
    def test_live_events_url_strips_trailing_slashes(self):
        replay_demo = load_replay_demo()

        self.assertEqual(
            replay_demo.live_events_url("http://127.0.0.1:4173/"),
            "http://127.0.0.1:4173/api/live/events",
        )
        self.assertEqual(
            replay_demo.live_events_url("http://127.0.0.1:4173///"),
            "http://127.0.0.1:4173/api/live/events",
        )


if __name__ == "__main__":
    unittest.main()
