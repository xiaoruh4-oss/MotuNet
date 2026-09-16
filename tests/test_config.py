import json
import math
import tempfile
import unittest
from pathlib import Path

from netlab.config import DEFAULT_CONFIG, build_filter, validate_config
from netlab.storage import ConfigStore


class ConfigTests(unittest.TestCase):
    def test_default_filter_excludes_loopback_and_impostor(self):
        self.assertIn("not loopback", DEFAULT_CONFIG["filter"])
        self.assertIn("not impostor", DEFAULT_CONFIG["filter"])

    def test_ipv6_endpoint_and_ports(self):
        value = validate_config({"scope": "endpoint", "host": "2001:db8::1", "ports": "80,443"})
        self.assertIn("ipv6.SrcAddr", value["filter"])
        self.assertIn("tcp.SrcPort == 80", value["filter"])
        self.assertIn("tcp.SrcPort == 443", value["filter"])

    def test_loopback_rejects_inbound(self):
        with self.assertRaises(ValueError):
            validate_config({"scope": "loopback", "direction": "inbound"})

    def test_endpoint_requires_target(self):
        with self.assertRaises(ValueError):
            validate_config({"scope": "endpoint"})

    def test_advanced_filter_guard(self):
        value = validate_config({"scope": "advanced", "advanced_filter": "tcp"})
        self.assertEqual(value["filter"], "(tcp) and not impostor")

    def test_unknown_and_invalid_values(self):
        with self.assertRaises(ValueError):
            validate_config({"unexpected": 1})
        for value in (True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                validate_config({"loss_pct": value})
        with self.assertRaises(ValueError):
            validate_config({"ports": "0,65536"})

    def test_blackout_timing_defaults_and_validation(self):
        self.assertEqual(DEFAULT_CONFIG["start_delay_s"], 0)
        self.assertEqual(DEFAULT_CONFIG["blackout_duration_s"], 10)
        cfg = validate_config({"blackout": True, "start_delay_s": 5, "blackout_duration_s": 3})
        self.assertEqual(cfg["start_delay_s"], 5)
        self.assertEqual(cfg["blackout_duration_s"], 3)
        with self.assertRaisesRegex(ValueError, "断网前等待时间必须是整数秒"):
            validate_config({"start_delay_s": 1.5})
        with self.assertRaisesRegex(ValueError, "断网持续时间必须是整数秒"):
            validate_config({"blackout_duration_s": True})
        with self.assertRaisesRegex(ValueError, "至少为1秒"):
            validate_config({"blackout": True, "start_delay_s": 2, "blackout_duration_s": 0})

    def test_legacy_blackout_duration_migrates(self):
        legacy = validate_config({"blackout": True, "duration_s": 30})
        self.assertEqual(legacy["duration_s"], 30)
        self.assertEqual(legacy["blackout_duration_s"], 30)
        manual = validate_config({"blackout": True, "duration_s": 0})
        self.assertEqual(manual["duration_s"], 0)
        self.assertEqual(manual["blackout_duration_s"], 0)
        self.assertFalse(legacy["blackout_loop"])
        self.assertFalse(manual["blackout_loop"])

    def test_blackout_loop_defaults_validation_and_roundtrip(self):
        self.assertFalse(DEFAULT_CONFIG["blackout_loop"])
        valid = {"blackout": True, "blackout_loop": True,
                 "start_delay_s": 2, "blackout_duration_s": 3}
        for changes in ({"blackout_loop": "true"}, {"blackout_loop": 1},
                        {"blackout": False}, {"start_delay_s": 0},
                        {"blackout_duration_s": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_config(dict(valid, **changes))
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(directory)
            store.save(valid)
            loaded = store.load()
            self.assertTrue(loaded["blackout_loop"])
            self.assertEqual(loaded["start_delay_s"], 2)
            self.assertEqual(loaded["blackout_duration_s"], 3)

    def test_json_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(directory)
            saved = store.save(DEFAULT_CONFIG)
            self.assertEqual(saved["filter"], DEFAULT_CONFIG["filter"])
            self.assertEqual(store.load()["name"], DEFAULT_CONFIG["name"])
            Path(store.path).write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                store.load()


if __name__ == "__main__":
    unittest.main()
