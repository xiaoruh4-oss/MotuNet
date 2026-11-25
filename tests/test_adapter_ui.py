import unittest
from unittest.mock import patch

from tests import test_ui as base_ui
from netlab.config import DEFAULT_CONFIG
from netlab.desktop_engine import DesktopEngine

GUID = "24D2BA8A-EDA6-4F28-83B8-1AB6563215BC"


class AdapterUiTests(unittest.TestCase):
    setUpClass = base_ui.DesktopTests.__dict__["setUpClass"]
    setUp = base_ui.DesktopTests.setUp
    tearDown = base_ui.DesktopTests.tearDown
    settle = base_ui.DesktopTests.settle

    def load_adapter(self):
        self.window.load_form(dict(DEFAULT_CONFIG, blackout=True, blackout_mode="adapter",
                                   adapter_ids=[GUID], start_delay_s=3, blackout_duration_s=5))

    def test_physical_outage_bypasses_packet_filter_and_is_saved(self):
        self.load_adapter()
        self.assertFalse(self.window.packet_target.isEnabled())
        self.assertFalse(self.window.blackout_loop.isEnabled())
        self.assertFalse(self.window.quick_down.isEnabled())
        with patch("netlab.ui.is_admin", return_value=True), \
                patch.object(self.engine, "validate_filter", side_effect=AssertionError("must not load WinDivert")):
            self.window.start_test()
            self.settle()
        self.assertEqual(self.engine.config["adapter_ids"], [GUID])
        self.assertEqual(self.window.store.load()["blackout_mode"], "adapter")
        self.engine.state.update(phase="active", adapter_names=["WLAN"], active_remaining=5)
        self.window.tick()
        self.assertIn("网卡已禁用", self.window.status.text())
        self.assertIn("WLAN", self.window.run_banner.text())
        self.assertNotIn("随机丢弃", self.window.run_banner.text())

    def test_empty_selection_does_not_start(self):
        self.load_adapter()
        self.window.adapter_ids = []
        with patch("netlab.ui.is_admin", return_value=True), patch.object(self.window, "error") as error:
            self.window.start_test()
        self.assertFalse(self.engine.state["running"])
        self.assertIn("选择", error.call_args.args[0])

    def test_restore_failure_does_not_claim_stopped_or_close(self):
        self.load_adapter()
        self.window.session_config = self.window.collect()
        self.window.session_active = True
        self.window.close_pending = True
        self.engine.state.update(running=True, phase="restore_failed", error="启用失败")
        with patch.object(self.window, "error"):
            self.window.job_done("stop", "启用失败")
        self.assertTrue(self.window.session_active)
        self.assertFalse(self.window.close_pending)
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertEqual(self.window.stop_button.text(), "重试恢复")
        self.assertFalse(self.window.start_button.isEnabled())

    def test_back_to_packet_retains_original_duration_range(self):
        self.load_adapter()
        self.window.load_form(dict(DEFAULT_CONFIG, blackout=True, blackout_mode="packet",
                                   blackout_duration_s=600, duration_s=600))
        self.assertEqual(self.window.collect()["blackout_duration_s"], 600)
        self.assertTrue(self.window.packet_target.isEnabled())
        self.assertIn("丢包断网", self.window.scene_summary())

    def test_pending_recovery_keeps_administrator_instruction_visible(self):
        self.engine.state.update(running=True, phase="restore_failed", recovery_pending=True,
                                 adapter_ids=[GUID], adapter_names=["WLAN"])
        with patch("netlab.ui.is_admin", return_value=False):
            self.window.recover_previous_adapters()
            self.window.tick()
        self.assertIn("等待管理员恢复", self.window.status.text())
        self.assertIn("以管理员身份重启", self.window.run_banner.text())
        self.assertTrue(self.window.admin_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())
        self.engine.state.update(running=False, recovery_pending=False)
        self.window.session_active = False


class DispatchTests(unittest.TestCase):
    def test_no_switch_before_successful_restore(self):
        from tests.test_ui import FakeEngine
        packet, adapter = FakeEngine(), FakeEngine()
        engine = DesktopEngine(".", packet=packet, adapter=adapter)
        config = dict(DEFAULT_CONFIG, blackout=True, blackout_mode="adapter", adapter_ids=[GUID])
        engine.start(config)
        self.assertIs(engine.active, adapter)
        with self.assertRaises(RuntimeError):
            engine.start(dict(DEFAULT_CONFIG))
        engine.stop()
        engine.start(dict(DEFAULT_CONFIG))
        self.assertIs(engine.active, packet)
        engine.stop()
