"""Exercise plan and editor controls without showing the desktop window."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QLabel, QLineEdit, QPlainTextEdit

from netlab.case_suite import new_run
from netlab.config import DEFAULT_CONFIG
from netlab.storage import AuditLog, ConfigStore
from netlab.ui import MainWindow
from tests.test_ui import FakeEngine


class BusinessSceneUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = MainWindow(engine=FakeEngine(), store=ConfigStore(self.temp.name),
                                 audit=AuditLog(self.temp.name))

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_invalid_plan_clears_stale_preview_and_disables_start(self):
        self.assertGreater(self.window.case_list.count(), 0)
        self.window.case_level.setCurrentIndex(self.window.case_level.findData("custom"))
        for action in self.window.case_condition_menu.actions():
            action.setChecked(False)
        self.assertEqual(self.window.case_list.count(), 0)
        self.assertFalse(self.window.case_start.isEnabled())
        self.assertIn("至少一个网络条件", self.window.case_plan_hint.text())
        self.window.case_condition_menu.actions()[0].setChecked(True)
        self.assertGreater(self.window.case_list.count(), 0)
        self.assertTrue(self.window.case_start.isEnabled())

    def test_case_rows_fit_two_lines_with_full_tooltip(self):
        item = self.window.case_list.item(0)
        self.assertEqual(len(item.text().splitlines()), 2)
        self.assertIn(self.window.case_preview[0]["title"], item.toolTip())
        metrics = self.window.case_list.fontMetrics()
        for line in item.text().splitlines():
            self.assertLessEqual(metrics.horizontalAdvance(line), max(120, self.window.case_list.viewport().width() - 24))

    def test_editor_keeps_invalid_input_open_until_valid_save(self):
        observed = {}

        class AutoDialog(QDialog):
            def exec(self):
                save = self.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save)
                error = self.findChild(QLabel, "scene_validation_error")
                save.click()
                observed["first_result"] = self.result()
                observed["first_error"] = error.text()
                self.findChild(QLineEdit).setText("背包奖励领取")
                for editor in self.findChildren(QPlainTextEdit):
                    editor.setPlainText("检查结果只生效一次")
                tiers = self.findChildren(QComboBox)
                for tier in tiers:
                    tier.setCurrentIndex(tier.findData("none"))
                save.click()
                observed["matrix_result"] = self.result()
                observed["matrix_error"] = error.text()
                tiers[0].setCurrentIndex(tiers[0].findData("smoke"))
                save.click()
                observed["final_result"] = self.result()
                return self.result()

        with patch("netlab.business_scene_ui.QDialog", AutoDialog):
            self.window.edit_business_scene(duplicate=True)
        self.assertEqual(observed["first_result"], QDialog.DialogCode.Rejected)
        self.assertIn("请填写场景名称", observed["first_error"])
        self.assertEqual(observed["matrix_result"], QDialog.DialogCode.Rejected)
        self.assertIn("至少选择一项", observed["matrix_error"])
        self.assertEqual(observed["final_result"], QDialog.DialogCode.Accepted)
        self.assertEqual(self.window.case_business.currentText(), "背包奖励领取")

    def test_failed_new_run_keeps_previous_report_available(self):
        self.window.case_system.setText("登录系统")
        previous = new_run(DEFAULT_CONFIG)
        previous["status"] = "已完成"
        previous_path = Path(self.temp.name) / "previous.json"
        self.window.case_run = previous
        self.window.case_report_path = previous_path
        self.window.case_index = len(previous["cases"]) - 1
        with patch("netlab.case_workflow.is_admin", return_value=True), \
             patch.object(self.window, "write_case_run", side_effect=OSError("磁盘已满")), \
             patch.object(self.window, "error") as error:
            self.window.start_case_session()
        self.assertIs(self.window.case_run, previous)
        self.assertEqual(self.window.case_report_path, previous_path)
        self.assertIn("磁盘已满", error.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
