import ctypes
import ipaddress
import json
import math
import os
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unittest.mock import patch
from netlab.config import DEFAULT_CONFIG, _resolve_web_dns, build_filter, resolve_web_config, validate_config
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

    def test_web_target_is_safe_until_start_and_defaults_https_port(self):
        value = validate_config({"scope": "web", "host": "https://Game.Example.com/play"})
        self.assertEqual(value["host"], "game.example.com")
        self.assertEqual(value["ports"], "443")
        self.assertIn("0.0.0.0", value["filter"])
        with patch("netlab.config.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("203.0.113.8", 0))]):
            resolved = resolve_web_config(value)
        self.assertEqual(resolved["web_ips"], ["203.0.113.8"])
        self.assertIn("ip.SrcAddr == 203.0.113.8", resolved["filter"])
        self.assertIn("tcp.DstPort == 443", resolved["filter"])

    def test_web_target_rejects_unresolved_domain(self):
        with patch("netlab.config.socket.getaddrinfo", side_effect=OSError("no DNS")):
            with self.assertRaisesRegex(ValueError, "无法解析"):
                resolve_web_config({"scope": "web", "host": "game.example.com"})

    def test_web_url_ports_and_literal_hosts_survive_normalization(self):
        for host, normalized, port in (
                ("http://localhost:7456/", "localhost", "7456"),
                ("http://game.example.com/play", "game.example.com", "80"),
                ("https://game.example.com/play", "game.example.com", "443"),
                ("ws://game.example.com/socket", "game.example.com", "80"),
                ("wss://game.example.com/socket", "game.example.com", "443"),
                ("wss://game.example.com:8443/socket", "game.example.com", "8443"),
                ("http://127.0.0.1:7456/play?token=x#scene", "127.0.0.1", "7456"),
                ("http://[::1]:7456/play", "::1", "7456"),
                ("https://[2001:db8::1]/", "2001:db8::1", "443")):
            with self.subTest(host=host):
                cfg = validate_config({"scope": "web", "host": host})
                self.assertEqual((cfg["host"], cfg["ports"]), (normalized, port))
                self.assertEqual(validate_config(cfg), cfg)
        with patch("netlab.config.socket.getaddrinfo", side_effect=AssertionError("literal must not query DNS")):
            cfg = resolve_web_config({"scope": "web", "host": "http://[::1]:7456/"})
        self.assertEqual(cfg["web_ips"], ["::1"])

    def test_web_url_rejects_credentials_invalid_ports_and_filter_fragments(self):
        for host in ("https://user:secret@game.example.com/", "https://user@game.example.com/",
                     "http://localhost:0/", "http://localhost:65536/", "http://[::1",
                     "http://game.example.com\\evil", "http://game.example.com/a\nb",
                     "http://[fe80::1%25eth0]/", "game.example.com.."):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_config({"scope": "web", "host": host})
        for value in (1, True, b"127.0.0.1", "127.0.0.1 or true", "::1%1", " 127.0.0.1"):
            for operation in (validate_config, build_filter):
                with self.subTest(value=value, operation=operation), self.assertRaises(ValueError):
                    operation({"scope": "web", "host": "localhost", "web_ips": [value]})

    def test_web_loopback_and_remote_filter_branches_keep_port_and_direction(self):
        resolved = [
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (23, 1, 6, "", ("::1", 0, 0, 0)),
            (2, 1, 6, "", ("203.0.113.8", 0)),
        ]
        with patch("netlab.config.socket.getaddrinfo", return_value=resolved):
            cfg = resolve_web_config({"scope": "web", "host": "http://localhost:7456/"})
        expression = cfg["filter"]
        self.assertIn("(loopback and outbound and (ip.DstAddr == 127.0.0.1", expression)
        self.assertIn("ipv6.DstAddr == ::1", expression)
        self.assertIn("(not loopback and inbound and (ip.SrcAddr == 203.0.113.8", expression)
        self.assertIn("tcp.DstPort == 7456", expression)
        self.assertIn("udp.DstPort == 7456", expression)
        down = validate_config(dict(cfg, direction="inbound"))["filter"]
        self.assertIn("loopback and outbound and (ip.SrcAddr == 127.0.0.1", down)
        self.assertIn("tcp.SrcPort == 7456", down)
        self.assertNotIn("tcp.DstPort", down)

    def test_web_inbound_is_preserved_for_remote_only_addresses(self):
        cfg = validate_config({"scope": "web", "host": "game.example.com",
                               "direction": "inbound", "web_ips": ["203.0.113.5"]})
        self.assertIn("not loopback and inbound", cfg["filter"])
        self.assertNotIn("and outbound", cfg["filter"])

    def test_web_whitespace_port_uses_default_and_direct_build_cannot_drop_port(self):
        value = validate_config(dict(scope="web", host="http://localhost:7456/", ports=" ",
                                      web_ips=["127.0.0.1"]))
        self.assertEqual(value["ports"], "7456")
        with self.assertRaisesRegex(ValueError, "服务器端口"):
            build_filter(dict(scope="web", host="localhost", web_ips=["127.0.0.1"]))

    def test_web_dns_deduplicates_but_does_not_silently_truncate(self):
        infos = [(2, 1, 6, "", (f"203.0.113.{n}", 0)) for n in range(1, 66)]
        with patch("netlab.config.socket.getaddrinfo", return_value=infos):
            with self.assertRaisesRegex(ValueError, "超过64"):
                resolve_web_config({"scope": "web", "host": "game.example.com"})
        with patch("netlab.config.socket.getaddrinfo", return_value=[infos[0], infos[0]]):
            cfg = resolve_web_config({"scope": "web", "host": "game.example.com"})
        self.assertEqual(cfg["web_ips"], ["203.0.113.1"])

    def test_web_dns_wait_is_bounded_even_when_os_resolver_stalls(self):
        release = threading.Event()
        done = threading.Event()
        def slow_resolver(*args, **kwargs):
            try:
                release.wait(2)
                return []
            finally:
                done.set()
        started = time.monotonic()
        try:
            with patch("netlab.config.socket.getaddrinfo", side_effect=slow_resolver):
                with self.assertRaisesRegex(ValueError, "超过8秒"):
                    _resolve_web_dns("game.example.com", timeout=.02)
                self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            self.assertTrue(done.wait(1))

    def test_web_target_requires_http_domain_and_rejects_malformed_labels(self):
        for host in ("ftp://game.example.com", "https://game..example.com", "https://-game.example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_config({"scope": "web", "host": host})
        with self.assertRaisesRegex(ValueError, "必须填写"):
            validate_config({"scope": "web", "ports": "443"})

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


@unittest.skipUnless(os.name == "nt", "WinDivert filter evaluation is Windows-only")
class WebDirectionFilterTests(unittest.TestCase):
    """Evaluate filters against packet bytes without opening/capturing traffic."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "vendor" / "windivert" / "WinDivert.dll"
        if not path.is_file():
            raise unittest.SkipTest("WinDivert helper DLL is unavailable")
        cls.dll = ctypes.WinDLL(str(path), use_last_error=True)
        cls.evaluate = cls.dll.WinDivertHelperEvalFilter
        cls.evaluate.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        cls.evaluate.restype = ctypes.c_int
        cls.compile = cls.dll.WinDivertHelperCompileFilter
        cls.compile.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_uint)]
        cls.compile.restype = ctypes.c_int

    def expression(self, target, port, direction, *, protocol="all", targets=None):
        host = f"http://[{target}]:{port}/" if ":" in target else f"http://{target}:{port}/"
        expression = validate_config(dict(scope="web", host=host, direction=direction,
                                          protocol=protocol, web_ips=targets or [target]))["filter"]
        error, offset = ctypes.c_char_p(), ctypes.c_uint()
        self.assertTrue(self.compile(expression.encode("ascii"), 0, None, 0,
                                     ctypes.byref(error), ctypes.byref(offset)),
                        f"Filter compile error at {offset.value}: {error.value}")
        return expression

    def matches(self, expression, source, source_port, destination, destination_port,
                *, outbound, loopback=False, protocol="tcp", impostor=False):
        source_ip, destination_ip = ipaddress.ip_address(source), ipaddress.ip_address(destination)
        self.assertEqual(source_ip.version, destination_ip.version)
        if protocol == "tcp":
            transport = struct.pack("!HHIIBBHHH", source_port, destination_port, 1, 1, 0x50, 0x18, 4096, 0, 0)
            number = 6
        else:
            transport = struct.pack("!HHHH", source_port, destination_port, 8, 0)
            number = 17
        if source_ip.version == 4:
            header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(transport), 1, 0,
                                 64, number, 0, source_ip.packed, destination_ip.packed)
        else:
            header = struct.pack("!IHBB16s16s", 6 << 28, len(transport), number, 64,
                                 source_ip.packed, destination_ip.packed)
        packet = ctypes.create_string_buffer(header + transport)
        address = bytearray(80)
        flags = ((int(outbound) << 17) | (int(loopback) << 18) |
                 (int(impostor) << 19) | (int(source_ip.version == 6) << 20))
        struct.pack_into("<I", address, 8, flags)
        metadata = ctypes.create_string_buffer(bytes(address), 80)
        return bool(self.evaluate(expression.encode("ascii"), packet, len(header + transport), metadata))

    def test_local_upload_download_match_server_port_with_both_packets_reported_outbound(self):
        for target in ("127.0.0.1", "::1"):
            for protocol in ("tcp", "udp"):
                for direction, expected_up, expected_down in (("outbound", True, False),
                                                               ("inbound", False, True),
                                                               ("both", True, True)):
                    with self.subTest(target=target, protocol=protocol, direction=direction):
                        expression = self.expression(target, 7456, direction)
                        upload = self.matches(expression, target, 51000, target, 7456,
                                              outbound=True, loopback=True, protocol=protocol)
                        download = self.matches(expression, target, 7456, target, 51000,
                                                outbound=True, loopback=True, protocol=protocol)
                        self.assertEqual(upload, expected_up)
                        self.assertEqual(download, expected_down)
                        self.assertFalse(self.matches(expression, target, 51000, target, 7457,
                                                      outbound=True, loopback=True, protocol=protocol))

    def test_remote_direction_requires_matching_server_ip_and_port_on_same_side(self):
        for server, client, other in (("203.0.113.8", "192.0.2.2", "198.51.100.9"),
                                      ("2001:db8::8", "2001:db8:1::2", "2001:db8:2::9")):
            for direction, expected_up, expected_down in (("outbound", True, False),
                                                           ("inbound", False, True),
                                                           ("both", True, True)):
                with self.subTest(server=server, direction=direction):
                    expression = self.expression(server, 443, direction)
                    self.assertEqual(self.matches(expression, client, 51000, server, 443,
                                                  outbound=True), expected_up)
                    self.assertEqual(self.matches(expression, server, 443, client, 51000,
                                                  outbound=False), expected_down)
                    # Old independent Src/Dst IP and Src/Dst port OR clauses
                    # matched these crossed endpoints, although neither targets
                    # the selected game server's port.
                    self.assertFalse(self.matches(expression, server, 51000, other, 443, outbound=True))
                    self.assertFalse(self.matches(expression, other, 443, server, 51000, outbound=False))
                    self.assertFalse(self.matches(expression, client, 443, server, 51000, outbound=True))
                    self.assertFalse(self.matches(expression, server, 51000, client, 443, outbound=False))
                    self.assertFalse(self.matches(expression, client, 51000, server, 443,
                                                  outbound=False))
                    self.assertFalse(self.matches(expression, server, 443, client, 51000,
                                                  outbound=True))

    def test_mixed_local_and_remote_targets_and_protocol_guard(self):
        targets = ["127.0.0.1", "::1", "203.0.113.8", "2001:db8::8"]
        expression = self.expression("127.0.0.1", 7456, "inbound", protocol="tcp", targets=targets)
        self.assertTrue(self.matches(expression, "127.0.0.1", 7456, "127.0.0.1", 51000,
                                     outbound=True, loopback=True))
        self.assertTrue(self.matches(expression, "2001:db8::8", 7456, "2001:db8:1::2", 51000,
                                     outbound=False))
        self.assertFalse(self.matches(expression, "127.0.0.1", 7456, "127.0.0.1", 51000,
                                      outbound=True, loopback=True, protocol="udp"))
        self.assertFalse(self.matches(expression, "127.0.0.1", 7456, "127.0.0.1", 51000,
                                      outbound=True, loopback=True, impostor=True))


if __name__ == "__main__":
    unittest.main()
