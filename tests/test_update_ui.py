"""Update UI lifecycle checks use no network and never launch an installer."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from netlab import __version__
from netlab.config import DEFAULT_CONFIG
from netlab.storage import AuditLog, ConfigStore
from netlab.ui import MainWindow
from netlab.updater import UpdateCancelled, UpdateInfo


class TestEngine:
    def __init__(self):
        self.running = False
        self.stopped = False
        self.fail_stop = False

    def snapshot(self):
        return {"running": self.running, "phase": "waiting", "cycle": 2}

    def stop(self):
        if self.fail_stop:
            raise OSError("模拟恢复失败")
        self.running = False
        self.stopped = True


class TestClient:
    def __init__(self, engine):
        self.engine = engine
        self.result = None
        self.error = None
        self.download_error = None
        self.check_calls = 0
        self.download_calls = 0
        self.check_gate = None
        self.download_gate = None
        self.download_entered = threading.Event()
        self.release = threading.Event()

    def check(self):
        self.check_calls += 1
        if self.check_gate:
            self.check_gate.wait(2)
        if self.error:
            raise self.error
        return self.result

    def download(self, info, directory, progress, cancel):
        assert self.engine.stopped and not self.engine.running
        self.download_calls += 1
        self.download_entered.set()
        if self.download_gate:
            self.download_gate.wait(2)
        if cancel.is_set():
            raise UpdateCancelled("cancelled")
        if self.download_error:
            raise self.download_error
        progress(100, 100)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "verified.exe"
        path.write_bytes(b"verified test download")
        return path


class UpdateDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = TestEngine()
        self.client = TestClient(self.engine)
        self.window = MainWindow(engine=self.engine, store=ConfigStore(self.temp.name),
            audit=AuditLog(self.temp.name), update_client=self.client, check_on_start=True)
        self.info = UpdateInfo("9.0.0", "https://github.com/example/MotuNet/releases/download/v9.0.0/MotuNet-9.0.0-Setup-x64.exe",
                               "a" * 64, 100, "新增更新功能 <b>纯文本</b>")

    def tearDown(self):
        for gate in (self.client.check_gate, self.client.download_gate):
            if gate:
                gate.set()
        self.engine.fail_stop = False
        self.engine.stop()
        self.window.update_cancel.set()
        self.wait(lambda: not self.window.update_busy and not self.window.busy)
        self.window.session_active = False
        if self.window.update_dialog is not None:
            self.window.update_dialog.close()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(predicate())

    def running_loop(self):
        self.engine.running = True
        self.window.session_active = True
        self.window.session_config = dict(DEFAULT_CONFIG, blackout=True, blackout_loop=True,
                                           start_delay_s=2, blackout_duration_s=3)
        self.window.set_running_controls()

    def test_startup_check_runs_once_without_blocking_window(self):
        self.client.check_gate = threading.Event()
        self.window.update_start_timer.start(1)
        self.wait(lambda: self.client.check_calls == 1)
        self.assertTrue(self.window.settings.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.check_updates(manual=True)
        self.assertEqual(self.client.check_calls, 1)
        self.client.check_gate.set()
        self.wait(lambda: not self.window.update_checking)
        self.assertIsNone(self.window.update_dialog)
        self.assertTrue(self.window.update_button.isEnabled())

    def test_manual_button_reports_latest_and_suppresses_pending_startup_check(self):
        self.window.update_button.click()
        self.wait(lambda: not self.window.update_checking)
        self.assertIn(__version__, self.window.update_dialog.text())
        self.assertFalse(self.window.update_start_timer.isActive())
        self.assertEqual(self.client.check_calls, 1)

    def test_startup_failure_silent_manual_failure_visible(self):
        self.client.error = OSError("连接超时")
        self.window.check_updates(manual=False)
        self.wait(lambda: not self.window.update_checking)
        self.assertIsNone(self.window.update_dialog)
        self.window.update_button.click()
        self.wait(lambda: not self.window.update_checking)
        self.assertIn("GitHub", self.window.update_dialog.text())
        self.assertTrue(self.window.settings.isEnabled())

    def test_auto_new_version_waits_for_running_loop_to_end(self):
        self.running_loop()
        self.client.result = self.info
        self.window.check_updates(manual=False)
        self.wait(lambda: not self.window.update_checking)
        self.assertIsNone(self.window.update_dialog)
        self.assertTrue(self.engine.running)
        self.engine.stop()
        self.window.finish_session("test")
        self.window.deliver_update_notice()
        self.assertIn("9.0.0", self.window.update_dialog.text())
        self.assertEqual(self.client.download_calls, 0)

    def test_manual_new_version_can_be_deferred_without_stopping_loop(self):
        self.running_loop()
        self.client.result = self.info
        self.window.check_updates(manual=True)
        self.wait(lambda: self.window.update_dialog is not None)
        self.assertIn("<b>纯文本</b>", self.window.update_dialog.informativeText())
        later = next(button for button in self.window.update_dialog.buttons() if button.text() == "稍后")
        later.click()
        self.assertTrue(self.engine.running)
        self.assertEqual(self.client.download_calls, 0)

    def test_update_stops_loop_downloads_then_launches_and_closes(self):
        self.running_loop()
        with patch("netlab.update_ui.launch_installer") as launch:
            self.window.start_update(self.info)
            self.wait(lambda: self.window.updates_closed)
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0].read_bytes(), b"verified test download")
        self.assertTrue(self.engine.stopped)
        self.assertEqual(self.client.download_calls, 1)

    def test_failed_stop_never_downloads_or_launches(self):
        self.running_loop()
        self.engine.fail_stop = True
        with patch.object(self.window, "error"), patch("netlab.update_ui.launch_installer") as launch:
            self.window.start_update(self.info)
            self.wait(lambda: not self.window.busy)
            self.assertFalse(self.window.update_busy)
            self.assertTrue(self.window.session_active)
            self.assertTrue(self.window.stop_button.isEnabled())
            self.assertEqual(self.client.download_calls, 0)
            launch.assert_not_called()

    def test_download_error_restores_controls_and_keeps_app_open(self):
        self.client.download_error = OSError("校验失败")
        with patch("netlab.update_ui.launch_installer") as launch:
            self.window.start_update(self.info)
            self.wait(lambda: not self.window.update_busy)
            launch.assert_not_called()
        self.assertIn("校验失败", self.window.update_dialog.text())
        self.assertFalse(self.window.updates_closed)
        self.assertTrue(self.window.settings.isEnabled())

    def test_cancel_download_does_not_launch(self):
        self.client.download_gate = threading.Event()
        with patch("netlab.update_ui.launch_installer") as launch:
            self.window.start_update(self.info)
            self.wait(lambda: self.client.download_entered.is_set())
            self.assertFalse(self.window.start_button.isEnabled())
            self.window.update_cancel.set()
            self.client.download_gate.set()
            self.wait(lambda: not self.window.update_busy)
            launch.assert_not_called()
        self.assertTrue(self.window.settings.isEnabled())
        self.assertIsNone(self.window.update_dialog)

    def test_close_during_download_cancels_then_closes_without_install(self):
        self.client.download_gate = threading.Event()
        with patch("netlab.update_ui.launch_installer") as launch:
            self.window.start_update(self.info)
            self.wait(lambda: self.client.download_entered.is_set())
            self.window.close()
            self.assertTrue(self.window.update_cancel.is_set())
            self.client.download_gate.set()
            self.wait(lambda: self.window.updates_closed)
            launch.assert_not_called()

    def test_installer_authorization_cancel_keeps_app_open(self):
        with patch("netlab.update_ui.launch_installer", side_effect=OSError("管理员授权已取消")):
            self.window.start_update(self.info)
            self.wait(lambda: not self.window.update_busy)
        self.assertFalse(self.window.updates_closed)
        self.assertIn("管理员授权已取消", self.window.update_dialog.text())
        self.assertTrue(self.window.settings.isEnabled())


if __name__ == "__main__":
    unittest.main()
