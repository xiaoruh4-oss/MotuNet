"""Desktop scene library actions; saving never starts a network test."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QListWidgetItem, QMessageBox

from .config import DEFAULT_CONFIG, PRESETS, validate_config
from .themes import stylesheet


def execution_signature(config):
    """Compare effective traffic settings, excluding names and inactive fields."""
    value = validate_config(config)
    if value["blackout"]:
        return (value["filter"], True, value["start_delay_s"], value["blackout_duration_s"], value["blackout_loop"])
    return (value["filter"], False, value["duration_s"], value["delay_ms"],
            value["jitter_ms"], value["loss_pct"], value["bandwidth_kbps"],
            value["duplicate_pct"], value["reorder_pct"],
            value["reorder_ms"] if value["reorder_pct"] else 0)


class SceneWorkflow:
    def initialize_scenes(self):
        self.selected_scenario_id = None
        self.selected_builtin_index = None
        self.refresh_scene_library()
        current = validate_config(self.collect())
        for entry in self.user_scenarios:
            if entry["config"] == current:
                self.selected_scenario_id = entry["id"]
                self.refresh_scene_library(entry["id"])
                break
        if self.selected_scenario_id is None:
            for index, preset in enumerate(PRESETS):
                if current["name"] == preset["name"]:
                    self.selected_builtin_index = index
                    self.presets.blockSignals(True)
                    self.presets.setCurrentRow(index)
                    self.presets.blockSignals(False)
                    break
        self.scene_form_changed()

    def refresh_scene_library(self, select_id=None):
        try:
            self.user_scenarios = self.store.list_scenarios()
        except OSError as exc:
            self.user_scenarios = []
            self.append_log(f"场景库读取失败：{exc}")
        self.presets.blockSignals(True)
        self.presets.clear()
        for preset in PRESETS:
            item = QListWidgetItem(preset["name"] + "\n" + preset["description"])
            item.setToolTip(preset["name"] + " · 内置场景\n" + preset["description"])
            self.presets.addItem(item)
        selected = -1
        for entry in self.user_scenarios:
            name = entry["config"]["name"]
            item = QListWidgetItem(name + "\n我的场景 · 已保存")
            item.setData(Qt.ItemDataRole.UserRole, entry["id"])
            item.setToolTip(name + "\n我的场景 · 含流量目标与全部网络条件")
            self.presets.addItem(item)
            if entry["id"] == select_id:
                selected = self.presets.count() - 1
        self.presets.setCurrentRow(selected)
        self.presets.blockSignals(False)
        for message in getattr(self.store, "scenario_errors", []):
            self.append_log(message)

    def select_preset(self, index):
        if self.loading or self.busy or index < 0:
            return
        if index < len(PRESETS):
            preset = PRESETS[index]
            data = self.collect()
            for key in (*self.numbers, "reorder_ms", "blackout", "duration_s",
                        "start_delay_s", "blackout_duration_s", "blackout_loop"):
                data[key] = DEFAULT_CONFIG[key]
            data.update(preset["values"])
            data["name"] = preset["name"]
            self.selected_scenario_id = None
            self.selected_builtin_index = index
            suffix = "；流量目标沿用当前设置。"
        else:
            rows = getattr(self, "user_scenarios", [])
            if index - len(PRESETS) >= len(rows):
                return
            entry = rows[index - len(PRESETS)]
            data = entry["config"]
            self.selected_scenario_id = entry["id"]
            self.selected_builtin_index = None
            suffix = "；已载入保存的流量目标与网络条件。"
        self.load_form(data)
        if self.session_active:
            suffix = ("；当前参数已生效。" if self.is_current_scene_running() else
                      "；点击“切换并启动”后生效。")
        self.append_log(f"已选择「{data['name']}」{suffix}")
        self.scene_form_changed()

    def is_current_scene_running(self):
        if not self.session_active or self.session_config is None:
            return False
        try:
            return execution_signature(self.collect()) == execution_signature(self.session_config)
        except (ValueError, TypeError):
            return False

    def update_selected_scene_name(self, *_):
        # Typing a draft title must never rename an immutable built-in template.
        self.scene_form_changed()

    def scene_form_changed(self, *_):
        if self.loading or not hasattr(self, "save_button"):
            return
        existing = next((entry for entry in getattr(self, "user_scenarios", [])
                         if entry["id"] == getattr(self, "selected_scenario_id", None)), None)
        if existing:
            try:
                dirty = validate_config(self.collect()) != existing["config"]
            except (ValueError, TypeError):
                dirty = True
            hint = "我的场景 · 有修改，点击保存更新" if dirty else "我的场景 · 已保存"
            self.save_button.setText("保存修改")
        elif getattr(self, "selected_builtin_index", None) is not None:
            hint = "内置场景 · 修改后可另存为自己的场景"
            self.save_button.setText("保存为新场景")
        else:
            hint = "自定义草稿 · 保存后加入左侧场景库"
            self.save_button.setText("保存场景")
        if hasattr(self, "scene_hint"):
            self.scene_hint.setText(hint)
        self.name_field.setToolTip(self.name_field.text() + "\n" + hint)
        self.set_running_controls()

    def suggest_scene_name(self, base):
        names = {preset["name"].casefold() for preset in PRESETS}
        names.update(row["config"]["name"].casefold() for row in getattr(self, "user_scenarios", []))
        base = base.strip()[:70] or "新建场景"
        result, count = base, 2
        while result.casefold() in names:
            result = f"{base} {count}"
            count += 1
        return result

    def _persist_scene(self, config, scenario_id=None):
        try:
            entry = self.store.save_scenario(config, scenario_id)
        except (OSError, ValueError) as exc:
            self.error(str(exc))
            return False
        self.selected_scenario_id = entry["id"]
        self.selected_builtin_index = None
        self.refresh_scene_library(entry["id"])
        self.load_form(entry["config"])
        try:
            self.store.save(entry["config"])
        except OSError as exc:
            self.append_log(f"场景已保存，但下次启动的默认配置未更新：{exc}")
        self.record("scenario_updated" if scenario_id else "scenario_created",
                    scenario_id=entry["id"], config=entry["config"])
        self.append_log(f"已保存「{entry['config']['name']}」到我的场景，下次打开仍可选择。")
        self.scene_form_changed()
        return True

    def new_scenario(self):
        if self.busy or self.session_active:
            return
        name, accepted = QInputDialog.getText(
            self, "新建场景", "场景名称（从正常网络开始，创建后可调整参数）",
            text=self.suggest_scene_name("新建场景"))
        if not accepted:
            return
        config = dict(DEFAULT_CONFIG)
        config.update(name=name.strip(), delay_ms=0, jitter_ms=0, loss_pct=0,
                      bandwidth_kbps=0, duplicate_pct=0, reorder_pct=0,
                      blackout=False, duration_s=0)
        self._persist_scene(config)

    def _confirm_delete_scenario(self, name):
        box = QMessageBox(self)
        box.setWindowTitle("删除场景")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"确定删除已保存的场景「{name}」？")
        box.setInformativeText("删除后无法撤销，当前未保存的修改也会放弃。删除后回到“正常对照”，不会启动测试。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("删除场景")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.setEscapeButton(QMessageBox.StandardButton.Cancel)
        box.setStyleSheet(stylesheet(self.theme_id) +
                         "\nQMessageBox QLabel#qt_msgboxex_icon_label { min-width: 0; }")
        return box.exec() == QMessageBox.StandardButton.Yes

    def delete_scenario(self):
        if self.busy or self.session_active or self.update_busy:
            return
        entry = next((row for row in self.user_scenarios
                      if row["id"] == self.selected_scenario_id), None)
        if entry is None or not self._confirm_delete_scenario(entry["config"]["name"]):
            return
        # Modal dialogs process events; an update may have started meanwhile.
        if (self.busy or self.session_active or self.update_busy or
                self.selected_scenario_id != entry["id"]):
            return
        try:
            self.store.delete_scenario(entry["id"])
        except (OSError, ValueError) as exc:
            self.error(str(exc))
            return
        self.selected_scenario_id = None
        self.selected_builtin_index = None
        self.refresh_scene_library()
        # Use the saved target, so an invalid unsaved draft cannot prevent
        # returning to a valid normal-network preset after deletion.
        self.load_form(entry["config"])
        normal_index = next(i for i, preset in enumerate(PRESETS) if preset["name"] == "正常对照")
        self.presets.setCurrentRow(normal_index)
        try:
            self.store.save(self.collect())
        except (OSError, ValueError) as exc:
            self.error(f"场景已删除，但下次启动的默认配置未更新：{exc}")
        self.record("scenario_deleted", scenario_id=entry["id"], name=entry["config"]["name"])
        self.append_log(f"已删除「{entry['config']['name']}」，已回到正常对照；未启动测试。")
        self.scene_form_changed()

    def save_as_scenario(self):
        if self.busy:
            return
        name, accepted = QInputDialog.getText(
            self, "另存为场景", "新场景名称（保留原场景和当前全部参数）",
            text=self.suggest_scene_name(self.name_field.text() + " 副本"))
        if not accepted:
            return
        config = self.collect()
        config["name"] = name.strip()
        self._persist_scene(config)

    def save_config(self):
        if self.busy or self.session_active:
            return
        config = self.collect()
        scene_id = getattr(self, "selected_scenario_id", None)
        if scene_id is None and config["name"].casefold() in {p["name"].casefold() for p in PRESETS}:
            self.save_as_scenario()
            return
        self._persist_scene(config, scene_id)
