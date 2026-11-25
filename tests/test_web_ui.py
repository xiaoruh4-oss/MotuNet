import unittest
from unittest.mock import patch

from tests import test_ui as base_ui
from netlab.config import DEFAULT_CONFIG, build_filter, validate_config
from netlab.scene_workflow import execution_signature


class WebUiTests(unittest.TestCase):
    setUpClass = base_ui.DesktopTests.__dict__["setUpClass"]
    setUp = base_ui.DesktopTests.setUp
    tearDown = base_ui.DesktopTests.tearDown
    settle = base_ui.DesktopTests.settle

    def web(self, host="game.example.com", **values):
        return validate_config(dict(DEFAULT_CONFIG, scope="web", host=host, **values))

    def test_target_identity_allows_switch_but_dns_cache_does_not_restart_same_target(self):
        first = self.web()
        resolved = dict(first, web_ips=["203.0.113.8"])
        self.assertEqual(execution_signature(first), execution_signature(resolved))
        self.assertNotEqual(execution_signature(first), execution_signature(self.web("other.example.com")))
        self.window.load_form(self.web("other.example.com"))
        self.window.session_config = resolved
        self.window.session_active = True
        self.engine.state["running"] = True
        self.window.set_running_controls()
        self.assertTrue(self.window.start_button.isEnabled())

    def test_leaving_web_does_not_validate_hidden_hostname_as_an_ip(self):
        self.window.load_form(self.web())
        self.window.scope.setCurrentIndex(self.window.scope.findData("all"))
        config = validate_config(self.window.collect())
        self.assertEqual(config["host"], "")
        self.assertEqual(config["ports"], "")

    def test_started_log_and_preview_show_actual_resolved_filter(self):
        config = self.web()
        resolved = dict(config, web_ips=["203.0.113.8"])
        resolved["filter"] = build_filter(resolved)
        self.window.load_form(config)
        self.engine.state["web_target"] = {key: resolved[key] for key in ("host", "ports", "web_ips", "filter")}
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
            self.settle()
        self.assertEqual(self.window.session_config["filter"], resolved["filter"])
        self.assertIn("203.0.113.8", self.window.filter_preview.text())
        self.assertIn("203.0.113.8", self.window.log_view.toPlainText())
        self.assertTrue(self.window.is_current_scene_running())
        self.assertFalse(self.window.start_button.isEnabled())

    def test_web_shortcut_selects_packet_loop_without_starting(self):
        self.window.prepare_web_outage()
        config = self.window.collect()
        self.assertEqual(config["scope"], "web")
        self.assertEqual(config["blackout_mode"], "packet")
        self.assertTrue(config["blackout_loop"])
        self.assertEqual((config["start_delay_s"], config["blackout_duration_s"]), (10, 5))
        self.assertFalse(self.engine.state["running"])

    def test_one_way_selection_reaches_engine_and_active_banner(self):
        for direction, caption in (("outbound", "仅上行"), ("inbound", "仅下行")):
            with self.subTest(direction=direction):
                self.window.load_form(self.web("http://localhost:7456/", blackout=True,
                                               blackout_duration_s=0))
                if direction == "outbound":
                    self.window.quick_down.setChecked(False)
                else:
                    self.window.quick_up.setChecked(False)
                with patch("netlab.ui.is_admin", return_value=True):
                    self.window.start_test()
                    self.settle()
                self.assertEqual(self.engine.config["direction"], direction)
                self.assertEqual(self.engine.config["ports"], "7456")
                self.engine.state.update(phase="active", received=2, dropped=2, blackout_dropped=2)
                self.window.tick()
                self.assertIn(caption + "断网", self.window.run_banner.text())
                self.assertIn("手动停止", self.window.quick_summary.text())
                self.assertIn("Cocos", self.window.scope_note.text())
                self.window.stop_test()
                self.settle()

    def test_web_target_note_changes_with_actual_target(self):
        self.window.load_form(self.web("http://localhost:7456/"))
        self.assertIn("本地服务 localhost:7456", self.window.scope_note.text())
        self.window.host.setText("wss://game.example.com/game")
        self.window.ports.clear()
        self.assertNotIn("本地服务", self.window.scope_note.text())
        self.assertIn("443", self.window.filter_preview.text())
