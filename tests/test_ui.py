"""Exercise the desktop workflow without opening an interception handle."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from netlab.config import DEFAULT_CONFIG, PRESETS, validate_config
from netlab.storage import ConfigStore, AuditLog
from netlab.ui import APP_NAME, MainWindow


class FakeEngine:
    def __init__(self):
        self.state = {"running": False, "received": 0, "sent": 0, "dropped": 0,
                      "queued": 0, "errors": 0, "error": None, "elapsed": 0}
        self.config = None
        self.calls = []
        self.fail_start = False

    def validate_filter(self, value):
        if not value:
            raise ValueError("empty filter")

    def start(self, config):
        self.calls.append(("start", config["name"]))
        if self.fail_start:
            raise RuntimeError("模拟启动失败")
        self.config = config
        self.state["running"] = True

    def stop(self):
        self.calls.append(("stop", None))
        self.state["running"] = False

    def snapshot(self):
        return dict(self.state)


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = FakeEngine()
        self.window = MainWindow(engine=self.engine, store=ConfigStore(self.temp.name),
                                 audit=AuditLog(self.temp.name))

    def tearDown(self):
        self.engine.stop()
        self.window.session_active = False
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def settle(self):
        deadline = time.monotonic() + 2
        while self.window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertFalse(self.window.busy)

    def test_start_locks_edits_stop_restores_and_audits(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.assertTrue(self.engine.state["running"])
        self.assertFalse(self.window.settings.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.window.stop_test()
        self.settle()
        self.assertFalse(self.engine.state["running"])
        self.assertTrue(self.window.settings.isEnabled())
        records = [json.loads(line) for line in self.window.audit.path.read_text(encoding="utf-8").splitlines()]
        self.assertIn("test_started", [row["event"] for row in records])
        self.assertIn("test_stopped", [row["event"] for row in records])

    def test_non_admin_does_not_start(self):
        with patch("netlab.ui.is_admin", return_value=False), patch.object(self.window, "error") as error:
            self.window.start_test()
        self.assertFalse(self.engine.state["running"])
        error.assert_called_once()
        message = error.call_args.args[0]
        self.assertIn("请先以管理员身份重启后使用", message)
        self.assertIn("以管理员身份重启", message)

    def test_non_admin_status_explains_restart_action(self):
        with patch("netlab.ui.is_admin", return_value=False):
            self.window.refresh_privilege()
        self.assertIn("请先以管理员身份重启后使用", self.window.admin_label.text())
        self.assertIn("以管理员身份重启", self.window.admin_label.toolTip())
        self.assertFalse(self.window.admin_button.isHidden())

    def test_admin_status_confirms_ready(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.refresh_privilege()
        self.assertIn("管理员权限已就绪", self.window.admin_label.text())
        self.assertTrue(self.window.admin_button.isHidden())

    def test_preset_selection_during_run_keeps_active_config(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        active_name = self.window.session_config["name"]
        next_index = 1 if len(PRESETS) > 1 else 0
        self.window.presets.setCurrentRow(next_index)
        self.assertEqual(self.window.session_config["name"], active_name)
        self.assertNotEqual(self.window.collect()["name"], active_name)
        self.assertTrue(self.window.start_button.isEnabled())

    def test_switch_stops_before_starting_new_scene(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.window.presets.setCurrentRow(1)
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.assertGreaterEqual(len(self.engine.calls), 3)
        self.assertEqual(self.engine.calls[-2][0], "stop")
        self.assertEqual(self.engine.calls[-1][0], "start")
        self.assertEqual(self.window.session_config["name"], self.window.collect()["name"])

    def test_switch_start_failure_does_not_claim_new_scene(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        old_name = self.window.session_config["name"]
        self.window.presets.setCurrentRow(1)
        new_name = self.window.collect()["name"]
        self.engine.fail_start = True
        with patch("netlab.ui.is_admin", return_value=True), patch.object(self.window, "error"):
            self.window.start_test()
            self.settle()
        self.assertNotEqual(old_name, new_name)
        self.assertFalse(self.window.session_active)
        self.assertIn("失败", self.window.status.text())

    def test_stop_during_switch_cancels_pending_start(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.window.presets.setCurrentRow(1)
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.window.stop_test()
        self.settle()
        self.assertFalse(self.window.session_active)
        self.assertNotIn(("start", self.window.collect()["name"]), self.engine.calls)

    def test_stop_initial_start_does_not_cancel_later_switch(self):
        """A stop during the first start must not leave a stale switch cancel flag."""
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.window.stop_test()
        self.settle()
        self.window.presets.setCurrentRow(1)
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.assertTrue(self.window.session_active)
        self.assertEqual(self.window.session_config["name"], self.window.collect()["name"])

    def test_scope_switch_drops_hidden_target(self):
        self.window.scope.setCurrentIndex(self.window.scope.findData("endpoint"))
        self.window.host.setText("192.0.2.25")
        self.window.ports.setText("9000")
        self.assertIn("192.0.2.25", self.window.filter_preview.text())
        self.window.scope.setCurrentIndex(self.window.scope.findData("all"))
        self.assertNotIn("192.0.2.25", self.window.filter_preview.text())
        self.assertNotIn("9000", self.window.filter_preview.text())

    def test_presets_are_valid_and_normal_has_no_impairments(self):
        for index, preset in enumerate(PRESETS):
            self.window.select_preset(index)
            self.assertEqual(validate_config(self.window.collect())["name"], preset["name"])
        normal_index = next(i for i, item in enumerate(PRESETS) if item["name"] == "正常对照")
        self.window.select_preset(normal_index)
        config = self.window.collect()
        for key in ("delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps", "duplicate_pct", "reorder_pct"):
            self.assertEqual(config[key], 0)
        self.assertFalse(config["blackout"])

    def test_auto_stop_unlocks_ui(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.engine.stop()
        self.window.tick()
        self.assertFalse(self.window.session_active)
        self.assertTrue(self.window.settings.isEnabled())


    def test_brand_name_constant_is_used(self):
        self.assertIn(APP_NAME, self.window.windowTitle())

    def test_save_does_not_start(self):
        self.window.name_field.setText("回归测试专用")
        self.window.save_config()
        self.assertEqual(self.window.store.load()["name"], "回归测试专用")
        self.assertFalse(self.engine.state["running"])

    def test_blackout_timing_form_and_countdown_phases(self):
        """The form carries N/M seconds and the status reflects each phase."""
        self.window.blackout.setChecked(True)
        self.window.blackout_delay.setValue(5)
        self.window.blackout_duration.setValue(3)
        config = validate_config(self.window.collect())
        self.assertTrue(config["blackout"])
        self.assertEqual(config["start_delay_s"], 5)
        self.assertEqual(config["blackout_duration_s"], 3)
        self.assertEqual(config["duration_s"], 3)
        self.assertTrue(self.window.blackout_delay.isEnabled())
        self.assertTrue(self.window.blackout_duration.isEnabled())
        self.assertFalse(self.window.duration.isEnabled())
        # Export/import style form sequence must preserve both timing values.
        self.window.load_form(config)
        reloaded = validate_config(self.window.collect())
        self.assertEqual(reloaded["start_delay_s"], 5)
        self.assertEqual(reloaded["blackout_duration_s"], 3)
        self.assertEqual(reloaded["duration_s"], 3)

        self.window.session_config = config
        self.window.session_active = True
        self.engine.state.update({"running": True, "phase": "waiting", "wait_remaining": 4.2})
        self.window.tick()
        self.assertIn("等待断网", self.window.status.text())
        self.assertIn("等待 5 秒后自动断网", self.window.run_note.text())

        self.engine.state.update({"phase": "active", "active_elapsed": 0.8, "active_remaining": 2.1})
        self.window.tick()
        self.assertIn("断网进行中", self.window.status.text())
        self.assertIn("剩余 3 秒", self.window.run_note.text())

    def test_blackout_zero_duration_is_manual_stop(self):
        self.window.blackout.setChecked(True)
        self.window.blackout_delay.setValue(0)
        self.window.blackout_duration.setValue(0)
        self.assertEqual(self.window.blackout_duration.specialValueText(), "手动停止")
        self.assertIn("手动停止", self.window.blackout_duration.toolTip())
        self.assertEqual(validate_config(self.window.collect())["duration_s"], 0)

    def test_running_scene_is_noop_without_resetting_audit_or_timer(self):
        self.window.presets.setCurrentRow(0)
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        calls = list(self.engine.calls)
        audit = self.window.audit.path.read_text(encoding="utf-8")
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertIn("当前场景运行中", self.window.start_button.text())
        self.assertTrue(self.window.stop_button.isEnabled())
        # Selecting away and back must not reset the session either.
        self.window.presets.setCurrentRow(1)
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.presets.setCurrentRow(0)
        self.assertFalse(self.window.start_button.isEnabled())
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.assertEqual(self.engine.calls, calls)
        self.assertEqual(self.window.audit.path.read_text(encoding="utf-8"), audit)

    def test_same_name_changed_parameters_can_switch(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        data = self.window.collect()
        data["loss_pct"] = 9
        self.window.load_form(data)
        self.assertTrue(self.window.start_button.isEnabled())
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.assertEqual(self.engine.config["loss_pct"], 9)
        self.assertEqual([call[0] for call in self.engine.calls[-2:]], ["stop", "start"])

    def test_scene_create_copy_update_and_reload(self):
        with patch("netlab.scene_workflow.QInputDialog.getText", return_value=("登录专项", True)):
            self.window.new_scenario()
        first_id = self.window.selected_scenario_id
        self.assertEqual(self.window.numbers["loss_pct"].value(), 0)
        self.window.numbers["loss_pct"].setValue(7)
        self.window.scope.setCurrentIndex(self.window.scope.findData("endpoint"))
        self.window.ports.setText("443")
        self.window.save_config()
        with patch("netlab.scene_workflow.QInputDialog.getText", return_value=("登录专项副本", True)):
            self.window.save_as_scenario()
        second_id = self.window.selected_scenario_id
        self.assertNotEqual(first_id, second_id)
        self.window.numbers["loss_pct"].setValue(12)
        self.window.save_config()
        rows = {row["id"]: row["config"] for row in ConfigStore(self.temp.name).list_scenarios()}
        self.assertEqual(rows[first_id]["loss_pct"], 7)
        self.assertEqual(rows[second_id]["loss_pct"], 12)
        self.assertEqual(rows[first_id]["ports"], "443")
        index = next(i for i, row in enumerate(self.window.user_scenarios) if row["id"] == first_id)
        self.window.presets.setCurrentRow(len(PRESETS) + index)
        self.assertEqual(self.window.collect()["loss_pct"], 7)
        self.assertEqual(self.window.collect()["ports"], "443")
        self.assertFalse(self.engine.calls)
        reloaded = MainWindow(engine=FakeEngine(), store=ConfigStore(self.temp.name), audit=AuditLog(self.temp.name))
        try:
            self.assertEqual(reloaded.presets.count(), len(PRESETS) + 2)
            self.assertEqual(reloaded.selected_scenario_id, second_id)
        finally:
            reloaded.close()
            reloaded.deleteLater()

    def test_scene_cancel_or_duplicate_never_overwrites_original(self):
        self.window.name_field.setText("我的原场景")
        self.window.save_config()
        original = self.window.store.list_scenarios()
        for method in (self.window.new_scenario, self.window.save_as_scenario):
            with patch("netlab.scene_workflow.QInputDialog.getText", return_value=("取消名称", False)):
                method()
        with patch("netlab.scene_workflow.QInputDialog.getText", return_value=("我的原场景", True)), patch.object(self.window, "error") as error:
            self.window.save_as_scenario()
            error.assert_called_once()
        self.assertEqual(self.window.store.list_scenarios(), original)

    def test_renaming_builtin_does_not_change_template(self):
        self.window.presets.setCurrentRow(0)
        self.window.name_field.setText("自己的轻度弱网")
        self.window.save_config()
        self.assertEqual(self.window.presets.item(0).text().splitlines()[0], PRESETS[0]["name"])
        self.window.presets.setCurrentRow(0)
        self.assertEqual(self.window.collect()["name"], PRESETS[0]["name"])

    def test_delete_cancel_uses_saved_name_and_preserves_unsaved_draft(self):
        self.window.name_field.setText("已保存的原名称")
        self.window.save_config()
        scene_id = self.window.selected_scenario_id
        saved = self.window.store.list_scenarios()
        self.window.name_field.setText("尚未保存的改名")
        self.window.numbers["loss_pct"].setValue(27)
        draft = self.window.collect()
        with patch.object(self.window, "_confirm_delete_scenario", return_value=False) as confirm:
            self.window.delete_scenario()
        confirm.assert_called_once_with("已保存的原名称")
        self.assertEqual(self.window.selected_scenario_id, scene_id)
        self.assertEqual(self.window.collect(), draft)
        self.assertEqual(self.window.store.list_scenarios(), saved)
        self.assertTrue(self.window.delete_button.isEnabled())
        self.assertEqual(self.engine.calls, [])

    def test_delete_confirmation_defaults_to_cancel_and_plain_text(self):
        observed = []

        def dismiss(box):
            observed.append(box.text())
            self.assertEqual(box.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(box.standardButton(box.defaultButton()), QMessageBox.StandardButton.Cancel)
            self.assertEqual(box.standardButton(box.escapeButton()), QMessageBox.StandardButton.Cancel)
            self.assertEqual(box.button(QMessageBox.StandardButton.Yes).text(), "删除场景")
            return QMessageBox.StandardButton.Cancel

        with patch("netlab.scene_workflow.QMessageBox.exec", new=dismiss):
            self.assertFalse(self.window._confirm_delete_scenario("<b>用户场景</b>"))
        self.assertEqual(observed, ["确定删除已保存的场景「<b>用户场景</b>」？"])

    def test_delete_failure_preserves_selection_draft_and_saved_data(self):
        self.window.name_field.setText("删除失败保留")
        self.window.save_config()
        scene_id = self.window.selected_scenario_id
        saved = self.window.store.list_scenarios()
        current = self.window.store.load()
        self.window.name_field.setText("未保存修改")
        draft = self.window.collect()
        with patch.object(self.window, "_confirm_delete_scenario", return_value=True), \
                patch.object(self.window.store, "delete_scenario", side_effect=ValueError("删除失败：没有权限")), \
                patch.object(self.window, "error") as error:
            self.window.delete_scenario()
        error.assert_called_once_with("删除失败：没有权限")
        self.assertEqual(self.window.selected_scenario_id, scene_id)
        self.assertEqual(self.window.collect(), draft)
        self.assertEqual(self.window.store.list_scenarios(), saved)
        self.assertEqual(self.window.store.load(), current)
        self.assertEqual(self.engine.calls, [])

    def test_delete_last_scene_returns_to_normal_and_persists_without_starting(self):
        self.window.load_form(dict(DEFAULT_CONFIG, name="最后一个循环场景", scope="endpoint",
                                   host="192.0.2.25", ports="9000", blackout=True,
                                   start_delay_s=2, blackout_duration_s=3, blackout_loop=True))
        self.window.save_config()
        scene_id = self.window.selected_scenario_id
        self.window.name_field.setText("无效草稿不应阻止删除")
        self.window.host.setText("not a valid host ???")
        with patch.object(self.window, "_confirm_delete_scenario", return_value=True):
            self.window.delete_scenario()
        normal = validate_config(self.window.collect())
        self.assertEqual(normal["name"], "正常对照")
        self.assertEqual(normal["host"], "192.0.2.25")
        self.assertEqual(normal["ports"], "9000")
        self.assertFalse(normal["blackout"])
        self.assertFalse(normal["blackout_loop"])
        for key in ("delay_ms", "jitter_ms", "loss_pct", "bandwidth_kbps", "duplicate_pct", "reorder_pct"):
            self.assertEqual(normal[key], 0)
        self.assertIsNone(self.window.selected_scenario_id)
        self.assertEqual(self.window.presets.count(), len(PRESETS))
        self.assertFalse(self.window.delete_button.isEnabled())
        self.assertEqual(ConfigStore(self.temp.name).list_scenarios(), [])
        self.assertEqual(ConfigStore(self.temp.name).load(), normal)
        self.assertEqual(self.engine.calls, [])
        records = [json.loads(line) for line in self.window.audit.path.read_text(encoding="utf-8").splitlines()]
        deleted = [row for row in records if row["event"] == "scenario_deleted"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["scenario_id"], scene_id)
        reloaded = MainWindow(engine=FakeEngine(), store=ConfigStore(self.temp.name), audit=AuditLog(self.temp.name))
        try:
            self.assertEqual(reloaded.collect()["name"], "正常对照")
            self.assertEqual(reloaded.presets.count(), len(PRESETS))
            self.assertFalse(reloaded.delete_button.isEnabled())
            self.assertEqual(reloaded.engine.calls, [])
        finally:
            reloaded.close()
            reloaded.deleteLater()

    def test_builtin_and_imported_draft_cannot_be_deleted(self):
        self.window.name_field.setText("应当保留的自定义场景")
        self.window.save_config()
        saved = self.window.store.list_scenarios()
        self.window.presets.setCurrentRow(0)
        with patch.object(self.window, "_confirm_delete_scenario") as confirm:
            self.assertFalse(self.window.delete_button.isEnabled())
            self.window.delete_scenario()
            path = Path(self.temp.name) / "draft.json"
            self.window.store.export(path, dict(DEFAULT_CONFIG, name="尚未保存的导入草稿"))
            with patch("netlab.ui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
                self.window.import_config()
            self.assertFalse(self.window.delete_button.isEnabled())
            self.window.delete_scenario()
            confirm.assert_not_called()
        self.assertEqual(self.window.store.list_scenarios(), saved)
        self.assertEqual(self.engine.calls, [])

    def test_delete_is_blocked_while_running_or_busy_or_updating(self):
        self.window.name_field.setText("繁忙时保留场景")
        self.window.save_config()
        saved = self.window.store.list_scenarios()
        for flag in ("session_active", "busy", "update_busy"):
            with self.subTest(flag=flag):
                setattr(self.window, flag, True)
                self.window.set_running_controls()
                with patch.object(self.window, "_confirm_delete_scenario") as confirm:
                    self.assertFalse(self.window.delete_button.isEnabled())
                    self.window.delete_scenario()
                    confirm.assert_not_called()
                self.assertEqual(self.window.store.list_scenarios(), saved)
                setattr(self.window, flag, False)
                self.window.set_running_controls()
                self.assertTrue(self.window.delete_button.isEnabled())
        self.assertEqual(self.engine.calls, [])

    def test_delete_rechecks_activity_and_selection_after_confirmation(self):
        self.window.name_field.setText("确认期间保留场景")
        self.window.save_config()
        scene_id = self.window.selected_scenario_id
        saved = self.window.store.list_scenarios()
        for attribute, value in (("session_active", True), ("busy", True),
                                 ("update_busy", True), ("selected_scenario_id", None)):
            with self.subTest(attribute=attribute):
                def change_state(_name):
                    setattr(self.window, attribute, value)
                    return True

                with patch.object(self.window, "_confirm_delete_scenario", side_effect=change_state):
                    self.window.delete_scenario()
                self.assertEqual(self.window.store.list_scenarios(), saved)
                setattr(self.window, attribute, scene_id if attribute == "selected_scenario_id" else False)
        self.assertEqual(self.engine.calls, [])

    def test_import_is_a_draft_and_cannot_overwrite_selected_user_scene(self):
        self.window.name_field.setText("保留的原场景")
        self.window.save_config()
        original_id = self.window.selected_scenario_id
        path = Path(self.temp.name) / "incoming.json"
        self.window.store.export(path, dict(DEFAULT_CONFIG, name="导入的场景", loss_pct=19))
        with patch("netlab.ui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.window.import_config()
        self.assertIsNone(self.window.selected_scenario_id)
        self.window.save_config()
        rows = {row["id"]: row["config"] for row in self.window.store.list_scenarios()}
        self.assertEqual(rows[original_id]["name"], "保留的原场景")
        self.assertEqual(len(rows), 2)

    def test_skin_persists_without_changing_active_session(self):
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        calls = list(self.engine.calls)
        config = self.window.session_config.copy()
        self.window.apply_theme("dark")
        self.assertEqual(self.window.theme_id, "dark")
        self.assertEqual(self.window.load_theme(), "dark")
        self.assertEqual(self.window.session_config, config)
        self.assertEqual(self.engine.calls, calls)
        self.assertFalse(self.window.start_button.isEnabled())

    def test_loop_option_defaults_off_and_requires_blackout(self):
        self.assertFalse(self.window.blackout_loop.isChecked())
        self.assertFalse(self.window.blackout_loop.isEnabled())
        self.window.load_form(dict(DEFAULT_CONFIG, blackout=True, start_delay_s=2,
                                   blackout_duration_s=3, blackout_loop=True))
        self.assertTrue(self.window.blackout_loop.isEnabled())
        self.assertIn("循环", self.window.blackout_hint.text())
        self.assertIn("2 秒", self.window.blackout_hint.text())
        self.window.blackout.setChecked(False)
        self.assertFalse(self.window.collect()["blackout_loop"])
        self.assertFalse(self.window.blackout_loop.isEnabled())
        self.assertFalse(validate_config(self.window.collect())["blackout"])

    def test_loop_persists_in_library_and_export_and_resets_with_preset(self):
        config = dict(DEFAULT_CONFIG, name="循环断网场景", blackout=True,
                      start_delay_s=2, blackout_duration_s=3, blackout_loop=True)
        self.window.load_form(config)
        self.window.save_config()
        scene_id = self.window.selected_scenario_id
        self.assertTrue(self.window.store.load()["blackout_loop"])
        saved = self.window.store.list_scenarios()[0]["config"]
        self.assertTrue(saved["blackout_loop"])
        self.window.select_preset(6)
        self.assertFalse(self.window.blackout_loop.isChecked())
        self.window.refresh_scene_library(scene_id)
        self.window.select_preset(self.window.presets.currentRow())
        self.assertTrue(self.window.blackout_loop.isChecked())
        path = Path(self.temp.name) / "loop.json"
        with patch("netlab.ui.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            self.window.export_config()
        self.window.select_preset(0)
        with patch("netlab.ui.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.window.import_config()
        reloaded = validate_config(self.window.collect())
        self.assertTrue(reloaded["blackout_loop"])
        self.assertEqual(reloaded["start_delay_s"], 2)
        self.assertEqual(reloaded["blackout_duration_s"], 3)

    def test_loop_rounds_show_state_without_restart_and_stop_restores(self):
        self.window.load_form(dict(DEFAULT_CONFIG, blackout=True, start_delay_s=2,
                                   blackout_duration_s=3, blackout_loop=True))
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        for phase in ("waiting", "active"):
            with self.subTest(phase=phase):
                self.engine.state.update(phase=phase, cycle=2, wait_remaining=1.2,
                                         active_remaining=2.1, active_elapsed=.9)
                self.window.tick()
                for widget in (self.window.status, self.window.run_banner, self.window.run_note):
                    self.assertIn("第 2 轮", widget.text())
                self.assertFalse(self.window.start_button.isEnabled())
                self.assertTrue(self.window.stop_button.isEnabled())
                calls = list(self.engine.calls)
                self.window.start_test()
                self.assertEqual(self.engine.calls, calls)
        # The loop flag changes execution semantics even if the timings match.
        self.window.blackout_loop.setChecked(False)
        self.assertFalse(self.window.is_current_scene_running())
        self.assertTrue(self.window.start_button.isEnabled())
        self.window.stop_test()
        self.settle()
        self.assertFalse(self.engine.state["running"])
        self.assertFalse(self.window.session_active)
        self.assertTrue(self.window.settings.isEnabled())

    def test_invalid_loop_timings_do_not_start_engine(self):
        for delay, duration in ((0, 3), (2, 0)):
            with self.subTest(delay=delay, duration=duration):
                self.window.load_form(dict(DEFAULT_CONFIG, blackout=True, start_delay_s=delay,
                                           blackout_duration_s=duration, blackout_loop=True))
                with patch("netlab.ui.is_admin", return_value=True), patch.object(self.window, "error") as error:
                    self.window.start_test()
                error.assert_called_once()
                self.assertFalse(self.engine.state["running"])
                self.assertEqual(self.engine.calls, [])

    def test_delayed_blackout_still_cannot_restart_while_waiting(self):
        self.window.presets.setCurrentRow(next(i for i, p in enumerate(PRESETS) if p["name"] == "延迟断网"))
        with patch("netlab.ui.is_admin", return_value=True):
            self.window.start_test()
        self.settle()
        self.engine.state.update(phase="waiting", wait_remaining=7)
        self.window.tick()
        calls = list(self.engine.calls)
        self.window.start_test()
        self.assertEqual(self.engine.calls, calls)
        self.assertIn("等待断网", self.window.status.text())
        self.assertTrue(self.window.advanced_toggle.isChecked())


if __name__ == "__main__":
    unittest.main()
