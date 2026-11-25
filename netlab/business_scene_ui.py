"""Desktop controls for reusable business-scene test plans."""
from __future__ import annotations

import uuid

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget,
)

from .case_suite import build_cases, budget_text
from .test_plan import (
    BusinessSceneStore, LEVEL_LABELS, build_test_plan, builtin_business_scenes,
    network_conditions,
)


class BusinessSceneWorkflow:
    def initialize_business_scenes(self):
        self.business_scene_store = BusinessSceneStore(self.store.root / "business-scenes")
        self.business_scenes = self.business_scene_store.list()
        for error in self.business_scene_store.errors:
            self.append_log(error)
        self.case_business.blockSignals(True)
        self.case_business.clear()
        for scene in self.business_scenes:
            self.case_business.addItem(scene["name"], scene["id"])
        self.case_business.addItem("全部业务场景", "all")
        self.case_business.blockSignals(False)
        self.case_business.setCurrentIndex(0)
        self.refresh_business_plan_preview()

    def selected_business_scenes(self):
        selected = self.case_business.currentData()
        if selected == "all":
            return self.business_scenes
        return [scene for scene in self.business_scenes if scene["id"] == selected]

    def selected_condition_ids(self):
        return [action.data() for action in self.case_condition_menu.actions() if action.isChecked()]

    def refresh_business_plan_preview(self, *_):
        if not hasattr(self, "case_mode") or not getattr(self, "business_scenes", None):
            return
        if self.case_run and self.case_run.get("status") == "进行中":
            return
        plan_mode = self.case_mode.currentData() == "plan"
        self.case_template_row.setVisible(not plan_mode)
        self.case_business_row.setVisible(plan_mode)
        self.case_level_row.setVisible(plan_mode)
        self.case_level.setEnabled(plan_mode)
        self.case_condition_button.setEnabled(plan_mode and self.case_level.currentData() == "custom")
        self.case_business_edit.setEnabled(plan_mode and self.case_business.currentData() not in
                                            ("all", "login", "lobby", "battle", "transaction"))
        if not plan_mode:
            try:
                preview = build_cases(self.collect(), self.current_template)
            except (TypeError, ValueError) as exc:
                self.clear_case_preview()
                self.case_plan_hint.setText(f"请检查流量目标：{exc}")
                return
            self.show_case_preview(preview)
            self.case_plan_hint.setText("模板流程由测试人员逐项确认结果。")
            return
        try:
            plan = self.current_business_plan()
        except (TypeError, ValueError) as exc:
            self.clear_case_preview()
            self.case_plan_hint.setText(f"请检查测试设置：{exc}")
            return
        self.case_plan_preview = plan
        self.show_case_preview(plan["cases"], plan_run=plan)
        self.case_plan_hint.setText(
            f"{LEVEL_LABELS[plan['plan_level']]} · {len(plan['business_scenes'])} 个业务场景 · "
            f"{len(plan['cases'])} 项 · {budget_text(plan['cases'])}"
        )

    def current_business_plan(self):
        scenes = self.selected_business_scenes()
        level = self.case_level.currentData()
        selected = self.selected_condition_ids() if level == "custom" else None
        return build_test_plan(self.collect(), scenes, level=level,
                               selected_condition_ids=selected,
                               system_name=self.case_system.text().strip())

    def start_case_session(self):
        if self.case_mode.currentData() == "template":
            self.start_case_flow()
            return
        name = self.case_system.text().strip()
        if not name:
            self.error("请先填写被测系统或功能名称。")
            self.case_system.setFocus()
            return
        try:
            plan = self.current_business_plan()
        except (TypeError, ValueError) as exc:
            self.error(f"无法生成本轮测试计划：{exc}")
            return
        self.start_case_flow(plan_run=plan)

    def import_business_scene(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入业务场景", "", "业务场景 (*.json)")
        if not path:
            return
        try:
            imported = self.business_scene_store.import_file(path)
        except (OSError, ValueError, TypeError) as exc:
            self.error(f"导入业务场景失败：{exc}")
            return
        self.initialize_business_scenes()
        target = imported[0]["id"]
        self.case_business.setCurrentIndex(self.case_business.findData(target))
        self.append_log(f"已导入 {len(imported)} 个业务场景。")

    def edit_business_scene(self, duplicate=False):
        current = self.selected_business_scenes()
        if not current:
            return
        source = current[0]
        editing = not duplicate and source["id"] not in {"login", "lobby", "battle", "transaction"}
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑业务场景" if editing else "新建业务场景")
        dialog.resize(760, 720)
        dialog.setStyleSheet(self.styleSheet())
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name = QLineEdit(source["name"] if editing else "")
        name.setPlaceholderText("例如：背包领取奖励")
        form.addRow("场景名称", name)
        edits = {}
        for field, title in (("preconditions", "前置条件"), ("steps", "业务操作"),
                             ("observations", "重点观察"), ("expected", "通过标准")):
            editor = QPlainTextEdit(source[field] if editing else "")
            editor.setFixedHeight(64)
            form.addRow(title, editor)
            edits[field] = editor
        layout.addLayout(form)
        layout.addWidget(QLabel("适用网络条件"))
        condition_scroll = QScrollArea()
        condition_scroll.setWidgetResizable(True)
        condition_scroll.setFixedHeight(235)
        container = QWidget()
        rows = QVBoxLayout(container)
        rows.setContentsMargins(5, 5, 5, 5)
        tiers = {}
        matrix = source["network_case_ids"]
        for condition in network_conditions():
            row = QHBoxLayout()
            title = QLabel(f"{condition['id']}  {condition['title']}")
            title.setMinimumWidth(220)
            row.addWidget(title, 1)
            tier = QComboBox()
            for text, value in (("冒烟起", "smoke"), ("标准起", "standard"),
                                ("完整起", "full"), ("不适用", "none")):
                tier.addItem(text, value)
            condition_id = condition["id"]
            value = next((level for level in ("smoke", "standard", "full")
                          if condition_id in matrix[level]), "none")
            tier.setCurrentIndex(tier.findData(value))
            tiers[condition_id] = tier
            row.addWidget(tier)
            rows.addLayout(row)
        rows.addStretch(1)
        condition_scroll.setWidget(container)
        layout.addWidget(condition_scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        validation_error = QLabel("")
        validation_error.setObjectName("scene_validation_error")
        validation_error.setWordWrap(True)
        validation_error.setStyleSheet("color: #ff8c97;" if self.theme_id == "dark" else "color: #b72b39;")
        layout.addWidget(validation_error)
        name.textChanged.connect(validation_error.clear)
        for editor in edits.values():
            editor.textChanged.connect(validation_error.clear)
        for tier in tiers.values():
            tier.currentIndexChanged.connect(validation_error.clear)
        saved_scene = None

        def save_scene():
            nonlocal saved_scene
            if not name.text().strip():
                validation_error.setText("请填写场景名称。")
                name.setFocus()
                return
            for field, title in (("preconditions", "前置条件"), ("steps", "业务操作"),
                                 ("observations", "重点观察"), ("expected", "通过标准")):
                if not edits[field].toPlainText().strip():
                    validation_error.setText(f"请填写{title}。")
                    edits[field].setFocus()
                    return
            matrix_out = {"smoke": [], "standard": [], "full": []}
            for condition_id, tier in tiers.items():
                value = tier.currentData()
                if value == "smoke":
                    matrix_out["smoke"].append(condition_id)
                if value in ("smoke", "standard"):
                    matrix_out["standard"].append(condition_id)
                if value != "none":
                    matrix_out["full"].append(condition_id)
            if not matrix_out["smoke"]:
                validation_error.setText("请至少选择一项从冒烟级别开始执行的网络条件。")
                condition_scroll.setFocus()
                return
            scene = {
                "schema_version": 1,
                "id": source["id"] if editing else f"custom-{uuid.uuid4().hex[:12]}",
                "name": name.text().strip(),
                **{key: editor.toPlainText().strip() for key, editor in edits.items()},
                "network_case_ids": matrix_out,
            }
            try:
                saved_scene = self.business_scene_store.save(scene)
            except (OSError, ValueError, TypeError) as exc:
                validation_error.setText(f"无法保存：{exc}")
                return
            dialog.accept()

        buttons.accepted.connect(save_scene)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if saved_scene is None:
            return
        self.initialize_business_scenes()
        self.case_business.setCurrentIndex(self.case_business.findData(saved_scene["id"]))
        self.append_log(f"已保存业务场景：{saved_scene['name']}")
