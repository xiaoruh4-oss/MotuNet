import unittest

from netlab.config import DEFAULT_CONFIG
from netlab.mobile_export import convert_desktop_scene, scene_catalog


class MobileExportTests(unittest.TestCase):
    def test_preserves_endpoint_and_network_values(self):
        desktop = dict(DEFAULT_CONFIG, name="登录弱网", scope="endpoint", host="203.0.113.5",
                       ports="443,8080", protocol="tcp", delay_ms=180.0,
                       bandwidth_kbps=128.0, blackout=True, start_delay_s=3,
                       blackout_duration_s=5)
        desktop.pop("filter")
        mobile = convert_desktop_scene(desktop)
        self.assertEqual(mobile["target"]["host"], "203.0.113.5")
        self.assertEqual(mobile["target"]["ports"], [443, 8080])
        self.assertEqual(mobile["impairment"]["bandwidth_kbps"], 128.0)
        self.assertEqual(mobile["impairment"]["start_delay_s"], 3)
        self.assertEqual(mobile["impairment"]["blackout_duration_s"], 5)
        self.assertEqual(mobile["impairment"]["duration_s"], 8)

    def test_maps_all_protocol_and_rejects_fractional_values(self):
        mobile = convert_desktop_scene(DEFAULT_CONFIG)
        self.assertEqual(mobile["target"]["protocol"], "both")
        self.assertIsInstance(mobile["impairment"]["delay_ms"], int)
        with self.assertRaisesRegex(ValueError, "小数参数 loss_pct"):
            convert_desktop_scene(dict(DEFAULT_CONFIG, loss_pct=1.5))
        with self.assertRaisesRegex(ValueError, "总时长"):
            convert_desktop_scene(dict(DEFAULT_CONFIG, blackout=True,
                                       start_delay_s=80_000, blackout_duration_s=10_000))

    def test_rejects_windows_only_filter(self):
        for scope in ("advanced", "loopback"):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                convert_desktop_scene(dict(DEFAULT_CONFIG, scope=scope))
        with self.assertRaisesRegex(ValueError, "WinDivert"):
            convert_desktop_scene(dict(DEFAULT_CONFIG, filter="tcp and outbound"))

    def test_catalog_does_not_mutate_input(self):
        original = dict(DEFAULT_CONFIG)
        result = scene_catalog([original])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(len(result["scenarios"]), 1)
        self.assertEqual(original, DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main()
