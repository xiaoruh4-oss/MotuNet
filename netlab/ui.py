"""Chinese desktop workflow for repeatable network impairment sessions."""
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import ipaddress
import math
import os
from pathlib import Path
import threading

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QUrl
from PySide6.QtGui import QActionGroup, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import APP_NAME, __version__
from .config import DEFAULT_CONFIG, PRESETS, build_filter, validate_config
from .desktop_engine import DesktopEngine
from .feedback import traffic_feedback
from .platform_win import elevate, is_admin, resource_dir
from .storage import AuditLog, ConfigStore
from .themes import THEMES, stylesheet
from .scene_workflow import SceneWorkflow
from .case_workflow import CaseWorkflow
from .business_scene_ui import BusinessSceneWorkflow
from .test_plan import network_conditions
from .update_ui import UpdateWorkflow

# Change this single value to rebrand the desktop application.
LOGO_PATH = "assets/motu-net-logo.png"

STYLE = stylesheet("blue")


class Jobs(QObject):
    done = Signal(str, object)


def label(text, name=None):
    result = QLabel(text)
    if name:
        result.setObjectName(name)
    return result


def panel(title):
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 15, 18, 16)
    layout.setSpacing(10)
    if title:
        layout.addWidget(label(title, "section"))
    return frame, layout


def combo(items):
    widget = QComboBox()
    for title, value in items:
        widget.addItem(title, value)
    widget.setToolTip(widget.currentText())
    widget.currentTextChanged.connect(widget.setToolTip)
    return widget


class ScenarioCard(QPushButton):
    def __init__(self, title, description, symbol, tone):
        super().__init__()
        self.setObjectName("scenario_card")
        self.setCheckable(True)
        self.setMinimumHeight(96)
        self.setToolTip(description)
        row = QHBoxLayout(self)
        row.setContentsMargins(17, 14, 15, 14)
        row.setSpacing(13)
        icon = label(symbol, "card_icon")
        icon.setProperty("tone", tone)
        icon.setFixedSize(48, 48)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(icon)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        copy.addWidget(label(title, "card_title"))
        copy.addWidget(label(description, "card_description"))
        row.addLayout(copy, 1)
        arrow = label("›", "card_chevron")
        row.addWidget(arrow)
        for child in (icon, arrow):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        for i in range(copy.count()):
            copy.itemAt(i).widget().setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


class MainWindow(UpdateWorkflow, SceneWorkflow, CaseWorkflow, BusinessSceneWorkflow, QMainWindow):
    def __init__(self, engine=None, store=None, audit=None, instance_lock=None,
                 update_client=None, check_on_start=False):
        super().__init__()
        self.engine = engine or DesktopEngine(resource_dir() / "vendor" / "windivert")
        self.adapter_ids = []
        self.adapter_names = {}
        self.store = store or ConfigStore()
        self.audit = audit or AuditLog()
        self.instance_lock = instance_lock
        self.busy = False
        self.session_active = False
        self.close_pending = False
        self.stop_pending = False
        self.cancel_switch = False
        self.pending_switch_config = None
        self.current_job = None
        self.loading = False
        self.last_error = ""
        self.session_config = None
        self.update_busy = False
        self.session_log = []
        self.init_case_workflow()
        self.help_dialog = None
        self.changelog_dialog = None
        self.jobs = Jobs()
        self.jobs.done.connect(self.job_done)
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(1320, 900)
        self.setMinimumSize(980, 680)
        self.theme_id = self.load_theme()
        self.setStyleSheet(stylesheet(self.theme_id))
        logo = resource_dir() / "assets" / "motu-net.ico"
        self.setWindowIcon(QIcon(str(logo)) if logo.exists() else self.make_icon())
        self.build_ui()
        self.hotkey_registered = False
        if os.name == "nt":
            self.hotkey_registered = bool(ctypes.windll.user32.RegisterHotKey(
                wintypes.HWND(int(self.winId())), 0x4e4c, 0x4003, 0x7b))
        self.hotkey_note.setText("快捷恢复  Ctrl + Alt + F12" if self.hotkey_registered
                                 else "快捷恢复未注册，请使用停止按钮")
        self.record_hotkey.setText("快捷恢复 Ctrl + Alt + F12；也可返回场景测试页停止。" if self.hotkey_registered
                                   else "快捷恢复未注册；请返回场景测试页使用停止按钮。")
        try:
            self.load_form(self.store.load())
        except (OSError, ValueError, TypeError) as exc:
            self.load_form(dict(DEFAULT_CONFIG))
            self.append_log(f"读取配置失败，已使用默认值：{exc}")
        if hasattr(self, "initialize_scenes"):
            try:
                self.initialize_scenes()
            except (OSError, ValueError, TypeError) as exc:
                self.append_log(f"场景库初始化失败：{exc}")
        if hasattr(self, "initialize_test_templates"):
            self.initialize_test_templates()
        self.initialize_business_scenes()
        self.refresh_privilege()
        self.name_field.setFocus()
        self.record("app_open", version=__version__, admin=is_admin())
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)
        self.timer.timeout.connect(self.refresh_theme_state)
        self.timer.start()
        self.initialize_updates(update_client, check_on_start)
        if self.engine.snapshot().get("recovery_pending"):
            QTimer.singleShot(0, self.recover_previous_adapters)

    def recover_previous_adapters(self):
        snapshot = self.engine.snapshot()
        self.session_config = dict(DEFAULT_CONFIG, name="恢复上次网卡断网",
                                   blackout=True, blackout_mode="adapter",
                                   adapter_ids=snapshot.get("adapter_ids", []))
        self.session_active = True
        self.set_running_controls()
        if is_admin():
            self.job("stop", self.engine.stop)
        else:
            self.run_banner.setText("检测到上次网卡恢复未完成，请先以管理员身份重启，程序将自动尝试恢复。")
            self.admin_button.setEnabled(True)
            self.status.setText("●  等待管理员恢复")

    def needs_adapter_recovery(self):
        return bool(self.engine.snapshot().get("recovery_pending"))

    @staticmethod
    def make_icon():
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor("#172c36"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#6be4c4"))
        painter.setBrush(QColor("#6be4c4"))
        for x, h in [(12, 15), (26, 27), (40, 40)]:
            painter.drawRoundedRect(x, 50-h, 9, h, 3, 3)
        painter.end()
        return QIcon(pixmap)

    def load_theme(self):
        try:
            payload = json.loads((self.store.root / "theme_settings.json").read_text(encoding="utf-8"))
            theme = payload.get("theme") if isinstance(payload, dict) else None
            return theme if theme in THEMES else "blue"
        except (OSError, ValueError, TypeError):
            return "blue"

    def apply_theme(self, theme, persist=True):
        if theme not in THEMES:
            return
        self.theme_id = theme
        self.setStyleSheet(stylesheet(theme))
        for dialog in (self.help_dialog, self.changelog_dialog):
            if dialog is not None:
                dialog.setStyleSheet(stylesheet(theme))
        self.theme_button.setText("皮肤 · " + THEMES[theme]["name"])
        for action in self.theme_menu.actions():
            action.setChecked(action.data() == theme)
        self.refresh_theme_state()
        if persist:
            try:
                path = self.store.root / "theme_settings.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps({"theme": theme}, ensure_ascii=False), encoding="utf-8")
                temp.replace(path)
            except OSError as exc:
                self.append_log(f"皮肤已切换，本次偏好未能保存：{exc}")

    def refresh_theme_state(self):
        for widget in (self.status, self.run_banner):
            if widget.property("active") != self.session_active:
                widget.setProperty("active", self.session_active)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
        self.run_banner.setVisible(self.session_active or self.busy)

    def toggle_advanced_section(self, checked):
        self.advanced_panel.setVisible(checked)
        self.advanced_toggle.setText(("▾" if checked else "▸") + "  高级选项 · 短时断网、重复与乱序")

    def sync_advanced_section(self):
        enabled = (self.blackout.isChecked() or self.numbers["duplicate_pct"].value() > 0
                   or self.numbers["reorder_pct"].value() > 0)
        self.advanced_toggle.setChecked(enabled)
        self.toggle_advanced_section(enabled)

    def show_view(self, index):
        self.views.setCurrentIndex(index)
        for button in (self.flow_nav, self.home_nav, self.detail_nav, self.record_nav):
            button.setChecked(button.property("view_index") == index)
        if index == 2:
            self.tick()

    def sync_quick_scene(self, *_):
        if not hasattr(self, "quick_scene") or self.loading:
            return
        index = self.presets.currentRow()
        self.quick_scene.blockSignals(True)
        if index < 0:
            draft = self.name_field.text().strip() or "自定义场景"
            if self.quick_scene.count() == self.presets.count():
                self.quick_scene.addItem(draft)
            else:
                self.quick_scene.setItemText(self.presets.count(), draft)
            self.quick_scene.setCurrentIndex(self.presets.count())
        else:
            if self.quick_scene.count() > self.presets.count():
                self.quick_scene.removeItem(self.presets.count())
            self.quick_scene.setCurrentIndex(index)
        self.quick_scene.blockSignals(False)
        for card_index, button in self.scene_cards.items():
            button.setChecked(card_index == index)
        self.quick_summary.setText(self.scene_summary())
        if hasattr(self, "quick_target"):
            data = self.collect()
            self.quick_target.setVisible(data["scope"] == "web")
            self.quick_target.setText(f"目标：{data['host'] or '请在详细设置填写游戏服务器地址'}" +
                                      (f" · 端口 {data['ports']}" if data["ports"] else ""))

    def refresh_quick_library(self):
        if not hasattr(self, "quick_scene"):
            return
        self.quick_scene.blockSignals(True)
        self.quick_scene.clear()
        for index in range(self.presets.count()):
            self.quick_scene.addItem(self.presets.item(index).text().splitlines()[0])
        self.quick_scene.blockSignals(False)
        self.sync_quick_scene()

    def select_quick_scene(self, index):
        if 0 <= index < self.presets.count():
            self.presets.setCurrentRow(index)
        self.sync_quick_scene()

    def scene_summary(self):
        data = self.collect()
        if data["blackout"]:
            wait = data["start_delay_s"]
            outage = data["blackout_duration_s"]
            mode = "网卡断网" if data.get("blackout_mode") == "adapter" else self.packet_direction(data) + "丢包断网"
            duration = f"持续 {outage} 秒" if outage else "手动停止"
            timing = f"{wait} 秒后断开 · {duration}" if wait else f"立即断开 · {duration}"
            return f"{mode} · {timing}"
        parts = []
        for key, unit in (("bandwidth_kbps", " KiB/s 带宽"), ("delay_ms", " ms 延迟"),
                          ("loss_pct", "% 丢包")):
            if data[key]:
                parts.append(f"{data[key]:g}{unit}")
        return " · ".join(parts) if parts else "正常网络对照"

    @staticmethod
    def packet_direction(config):
        if config.get("scope") == "loopback":
            return "全部回环流量"
        if config.get("scope") == "advanced":
            return "自定义过滤"
        return {"outbound": "仅上行", "inbound": "仅下行", "both": "双向"}[config["direction"]]

    def web_scope_note(self, config):
        host = config["host"]
        local = host == "localhost"
        try:
            local = local or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if local:
            return (f"当前只拦截本地服务 {host}:{config['ports']}。Cocos 页面加载后可能连接远程游戏服，"
                    "请填写实际游戏服 / WebSocket 地址才能测试游戏请求。上传=发往服务端，下载=服务端返回。")
        return ("上传=发往目标服务器，下载=目标服务器返回；只处理匹配 IP 和端口。"
                "不会禁用网卡；共享 IP 和端口的其他服务也可能受影响。每次开始时重新解析域名。")

    def sync_quick_direction(self, *_):
        if not hasattr(self, "quick_up") or self.loading:
            return
        direction = self.direction.currentData()
        local_all = self.scope.currentData() == "loopback"
        for check, checked in ((self.quick_up, local_all or direction != "inbound"),
                               (self.quick_down, local_all or direction != "outbound")):
            check.blockSignals(True)
            check.setChecked(checked)
            check.blockSignals(False)

    def change_quick_direction(self):
        up, down = self.quick_up.isChecked(), self.quick_down.isChecked()
        if not (up or down):
            self.sync_quick_direction()
            return
        value = "both" if up and down else "outbound" if up else "inbound"
        self.direction.setCurrentIndex(self.direction.findData(value))

    def build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(232)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 28, 18, 18)
        side.setSpacing(12)
        brand_row = QHBoxLayout()
        logo = label("")
        pixmap = QPixmap(str(resource_dir() / LOGO_PATH))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
            brand_row.addWidget(logo)
        brand_row.addWidget(label(APP_NAME, "brand"), 1)
        side.addLayout(brand_row)
        side.addSpacing(34)
        self.flow_nav = QPushButton("▦    弱网用例")
        self.home_nav = QPushButton("◈    场景测试")
        self.detail_nav = QPushButton("⚙    详细设置")
        self.record_nav = QPushButton("◷    测试记录")
        for button, index in ((self.flow_nav, 3), (self.home_nav, 0),
                              (self.detail_nav, 1), (self.record_nav, 2)):
            button.setObjectName("nav")
            button.setCheckable(True)
            button.setProperty("view_index", index)
            button.setMinimumHeight(50)
            button.clicked.connect(lambda checked=False, page=index: self.show_view(page))
            side.addWidget(button)
        # Keep saved case data private while focusing on direct testing.
        self.flow_nav.hide()
        side.addStretch(1)
        self.presets = QListWidget()
        self.presets.setWordWrap(True)
        self.presets.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.presets.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.presets.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for preset in PRESETS:
            item = QListWidgetItem(preset["name"] + "\n" + preset["description"])
            item.setToolTip(preset["description"])
            self.presets.addItem(item)
        self.presets.currentRowChanged.connect(self.select_preset)
        self.presets.currentRowChanged.connect(self.sync_quick_scene)
        self.scene_tools = QWidget()
        tools_layout = QVBoxLayout(self.scene_tools)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(10)
        tools_layout.addWidget(label("场景库", "section"))
        tools_layout.addWidget(label("内置场景与已保存的自定义场景", "muted"))
        tools_layout.addWidget(self.presets)
        self.presets.setMinimumHeight(185)
        scene_actions = QHBoxLayout()
        self.new_button = QPushButton("新建场景")
        self.new_button.clicked.connect(lambda: self.new_scenario())
        scene_actions.addWidget(self.new_button)
        self.delete_button = QPushButton("删除场景")
        self.delete_button.clicked.connect(lambda: self.delete_scenario())
        scene_actions.addWidget(self.delete_button)
        tools_layout.addLayout(scene_actions)
        transfer = QHBoxLayout()
        self.import_button = QPushButton("导入场景")
        self.import_button.clicked.connect(self.import_config)
        transfer.addWidget(self.import_button)
        self.export_button = QPushButton("导出场景")
        self.export_button.clicked.connect(self.export_config)
        transfer.addWidget(self.export_button)
        tools_layout.addLayout(transfer)
        help_row = QHBoxLayout()
        for title, callback in [("使用说明", self.show_help), ("更新日志", self.show_changelog)]:
            button = QPushButton(title)
            button.setObjectName("link")
            button.clicked.connect(callback)
            help_row.addWidget(button)
        side.addLayout(help_row)
        self.update_button = QPushButton("检查更新")
        self.update_button.clicked.connect(lambda: self.check_updates(manual=True))
        side.addWidget(self.update_button)
        self.theme_button = QPushButton("皮肤 · " + THEMES[self.theme_id]["name"])
        self.theme_menu = QMenu(self)
        theme_group = QActionGroup(self.theme_menu)
        theme_group.setExclusive(True)
        for theme_id, spec in THEMES.items():
            action = self.theme_menu.addAction(spec["name"])
            action.setCheckable(True)
            action.setData(theme_id)
            action.setChecked(theme_id == self.theme_id)
            action.triggered.connect(lambda checked=False, value=theme_id: self.apply_theme(value))
            theme_group.addAction(action)
        self.theme_button.setMenu(self.theme_menu)
        side.addWidget(self.theme_button)
        side.addSpacing(4)
        side.addWidget(label(f"v{__version__}  ·  Windows x64", "muted"))
        outer.addWidget(sidebar)

        right = QVBoxLayout()
        right.setContentsMargins(26, 28, 26, 20)
        right.setSpacing(14)
        header = QHBoxLayout()
        header.addStretch(1)
        self.status = label("●  待启动", "badge")
        header.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        right.addLayout(header)
        self.run_banner = label("当前未运行弱网场景", "run_banner")
        self.run_banner.setWordWrap(True)
        right.addWidget(self.run_banner)

        self.views = QStackedWidget()
        home = QWidget()
        home_layout = QVBoxLayout(home)
        home_layout.setContentsMargins(0, 8, 0, 0)
        home_layout.setSpacing(12)
        scene_heading = QHBoxLayout()
        scene_heading.addWidget(label("选择测试场景", "section"), 1)
        self.web_shortcut = QPushButton("网页定向断网")
        self.web_shortcut.clicked.connect(self.prepare_web_outage)
        scene_heading.addWidget(self.web_shortcut)
        self.adapter_shortcut = QPushButton("网卡断网 · 系统禁用")
        self.adapter_shortcut.setObjectName("primary")
        self.adapter_shortcut.clicked.connect(self.prepare_adapter_outage)
        scene_heading.addWidget(self.adapter_shortcut)
        home_layout.addLayout(scene_heading)
        card_content = QWidget()
        card_content.setObjectName("scroll_content")
        cards = QGridLayout(card_content)
        cards.setContentsMargins(0, 0, 0, 0)
        cards.setHorizontalSpacing(14)
        cards.setVerticalSpacing(14)
        self.scene_cards = {}
        for position, (preset_index, symbol, color) in enumerate([
            (0, "↯", "blue"), (2, "◉", "orange"), (4, "▥", "green"),
            (5, "⌁", "violet"), (3, "◌", "rose"), (6, "◷", "blue")
        ]):
            preset = PRESETS[preset_index]
            button = ScenarioCard(preset["name"], preset["description"], symbol, color)
            button.clicked.connect(lambda checked=False, row=preset_index: self.select_quick_scene(row))
            cards.addWidget(button, position // 3, position % 3)
            self.scene_cards[preset_index] = button
        for col in range(3):
            cards.setColumnStretch(col, 1)
        # Keep the start/restore controls readable on short windows.  Only the
        # scene gallery scrolls when the active-session banner needs more room.
        self.scene_card_scroll = QScrollArea()
        self.scene_card_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scene_card_scroll.setWidgetResizable(True)
        self.scene_card_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scene_card_scroll.setMinimumHeight(84)
        self.scene_card_scroll.setMaximumHeight(206)
        self.scene_card_scroll.setWidget(card_content)
        home_layout.addWidget(self.scene_card_scroll, 1)
        home_layout.addSpacing(4)
        home_layout.addWidget(label("快速启动", "section"))

        controls, bar = panel(None)
        controls.setObjectName("control_card")
        controls.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        actions = QHBoxLayout()
        actions.setSpacing(16)
        scene = QVBoxLayout()
        scene.addWidget(label("当前场景", "muted"))
        self.quick_scene = QComboBox()
        self.quick_scene.setObjectName("quick_scene")
        self.quick_scene.setMinimumHeight(46)
        self.quick_scene.blockSignals(True)
        for preset in PRESETS:
            self.quick_scene.addItem(preset["name"])
        self.quick_scene.blockSignals(False)
        self.quick_scene.currentIndexChanged.connect(self.select_quick_scene)
        scene.addWidget(self.quick_scene)
        self.quick_summary = label("", "muted")
        self.quick_summary.setWordWrap(True)
        scene.addWidget(self.quick_summary)
        self.quick_target = label("", "muted")
        self.quick_target.setWordWrap(True)
        scene.addWidget(self.quick_target)
        actions.addLayout(scene, 2)
        direction = QVBoxLayout()
        direction.addWidget(label("流量方向", "muted"))
        direction_row = QHBoxLayout()
        self.quick_up = QCheckBox("上传")
        self.quick_down = QCheckBox("下载")
        self.quick_up.setToolTip("阻断发往目标服务器的数据包；客户端仍可能显示发送记录，恢复后 TCP 可能重传。")
        self.quick_down.setToolTip("阻断目标服务器返回的数据包；上行请求仍可能被服务端处理。")
        for check in (self.quick_up, self.quick_down):
            check.toggled.connect(self.change_quick_direction)
            direction_row.addWidget(check)
        direction.addLayout(direction_row)
        actions.addLayout(direction, 1)
        self.name_field = QLineEdit()
        self.name_field.setObjectName("scene_name")
        self.name_field.setPlaceholderText("输入场景名称")
        self.name_field.setToolTip("修改名称后保存；使用“另存为”将当前参数保留为新的场景。")
        self.name_field.setMinimumWidth(80)
        self.start_button = QPushButton("▶  开始测试")
        self.start_button.setObjectName("start")
        self.start_button.setMinimumHeight(44)
        self.start_button.clicked.connect(self.start_test)
        self.stop_button = QPushButton("■  停止并恢复")
        self.stop_button.setObjectName("stop")
        self.stop_button.setMinimumHeight(44)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_test)
        action_column = QVBoxLayout()
        action_column.setSpacing(7)
        action_column.addWidget(self.start_button)
        action_column.addWidget(self.stop_button)
        actions.addLayout(action_column, 0)
        bar.addLayout(actions)
        self.scene_hint = label("可编辑参数，另存为自己的测试场景。", "scene_hint")
        self.scene_hint.setWordWrap(True)
        self.scene_hint.hide()
        privilege = QHBoxLayout()
        self.admin_label = label("", "admin_status")
        self.admin_label.setWordWrap(True)
        privilege.addWidget(self.admin_label, 1)
        self.admin_button = QPushButton("以管理员身份重启")
        self.admin_button.clicked.connect(self.request_admin)
        privilege.addWidget(self.admin_button)
        bar.addLayout(privilege)
        info = QHBoxLayout()
        self.run_note = label("尚未拦截流量", "muted")
        self.run_note.setWordWrap(True)
        info.addWidget(self.run_note, 1)
        self.hotkey_note = label("", "muted")
        self.hotkey_note.setWordWrap(True)
        info.addWidget(self.hotkey_note)
        self.run_info = QWidget()
        self.run_info.setLayout(info)
        bar.addWidget(self.run_info)
        home_layout.addWidget(controls)
        home_layout.addStretch(0)
        self.views.addWidget(home)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("scroll_content")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)
        body.addWidget(self.scene_tools)
        self.settings = QWidget()
        settings_layout = QVBoxLayout(self.settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(14)
        settings_layout.addWidget(label("场景名称", "muted"))
        settings_layout.addWidget(self.name_field)
        target, target_layout = panel("1   流量目标")
        self.packet_target = target
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.scope = combo([("整台电脑（非回环）", "all"), ("指定 IP / 端口", "endpoint"),
                            ("指定网页 / 域名", "web"),
                            ("本机回环 · localhost", "loopback"), ("高级过滤表达式", "advanced")])
        self.protocol = combo([("全部 IP（含 TCP / UDP）", "all"), ("仅 TCP", "tcp"), ("仅 UDP", "udp")])
        self.direction = combo([("上行 + 下行", "both"), ("仅上行（发送）", "outbound"), ("仅下行（接收）", "inbound")])
        for col, (title, field) in enumerate([("作用范围", self.scope), ("网络协议", self.protocol), ("流量方向", self.direction)]):
            field.setMinimumContentsLength(9)
            field.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            grid.addWidget(label(title, "muted"), 0, col)
            grid.addWidget(field, 1, col)
            grid.setColumnStretch(col, 1)
        self.host = QLineEdit()
        self.host.setPlaceholderText("服务器 IP 或网页地址，例如 game.example.com")
        self.ports = QLineEdit()
        self.ports.setPlaceholderText("端口，例如 443, 8080")
        grid.addWidget(self.host, 2, 0, 1, 2)
        grid.addWidget(self.ports, 2, 2)
        target_layout.addLayout(grid)
        self.advanced = QLineEdit()
        self.advanced.setPlaceholderText("WinDivert 表达式，例如 udp and outbound and udp.DstPort == 9000")
        target_layout.addWidget(self.advanced)
        self.scope_note = label("", "muted")
        self.scope_note.setWordWrap(True)
        target_layout.addWidget(self.scope_note)
        self.filter_preview = QLineEdit()
        self.filter_preview.setObjectName("filter_preview")
        self.filter_preview.setReadOnly(True)
        self.filter_preview.setToolTip("实际生效的过滤表达式；只影响匹配的数据包")
        target_layout.addWidget(self.filter_preview)
        settings_layout.addWidget(target)

        impairment, impairment_layout = panel("2   网络条件")
        fields = [
            ("delay_ms", "基础延迟", " ms", 0, 15000, 10, "每个匹配包的附加单程延迟。双向 150 ms 通常增加约 300 ms 往返时延。"),
            ("jitter_ms", "抖动 ±", " ms", 0, 15000, 5, "每包在 ± 抖动范围随机取值，最终延迟最低为 0。抖动本身也可能造成乱序。"),
            ("loss_pct", "随机丢包", " %", 0, 100, 1, "每个匹配包独立按概率丢弃，包括 TCP 确认包。"),
            ("bandwidth_kbps", "上下行分别限制", " KiB/s", 0, 1000000, 16, "0 表示不限速。1 KiB/s = 1024 字节/秒，上下行分别计算，超限排队。"),
            ("duplicate_pct", "重复包概率", " %", 0, 100, 1, "命中后额外发送 1 份数据包。"),
            ("reorder_pct", "乱序触发概率", " %", 0, 100, 1, "命中的包额外暂存，让后续包先通过；实际乱序取决于后续流量。"),
        ]
        self.numbers = {}
        for key, title, suffix, minimum, maximum, step, tip in fields:
            field = QDoubleSpinBox()
            field.setRange(minimum, maximum)
            field.setDecimals(1)
            field.setSingleStep(step)
            field.setSuffix(suffix)
            field.setToolTip(tip)
            field.setKeyboardTracking(False)
            field.setMinimumWidth(80)
            field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            self.numbers[key] = field
        common = QHBoxLayout()
        common.setSpacing(10)
        for title, keys, hint, stretch in [
            ("↓  带宽限制", ["bandwidth_kbps"], "0 表示不限速", 11),
            ("◷  延迟", ["delay_ms", "jitter_ms"], "影响每个匹配包的单程时延", 18),
            ("◇  丢包率", ["loss_pct"], "每个数据包独立计算", 10),
        ]:
            card, card_layout = panel(None)
            card.setObjectName("condition_card")
            card_layout.setContentsMargins(12, 12, 12, 12)
            card_layout.addWidget(label(title, "condition_title"))
            row = QGridLayout()
            for col, key in enumerate(keys):
                row.addWidget(label(next(f[1] for f in fields if f[0] == key), "muted"), 0, col)
                row.addWidget(self.numbers[key], 1, col)
                row.setColumnStretch(col, 1)
            card_layout.addLayout(row)
            hint_label = label(hint, "muted")
            hint_label.setWordWrap(True)
            card_layout.addWidget(hint_label)
            common.addWidget(card, stretch)
        impairment_layout.addLayout(common)
        self.advanced_toggle = QPushButton("▸  高级选项 · 短时断网、重复与乱序")
        self.advanced_toggle.setObjectName("advanced_toggle")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.toggled.connect(self.toggle_advanced_section)
        impairment_layout.addWidget(self.advanced_toggle)
        self.advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 2, 0, 6)
        advanced_layout.setSpacing(10)
        self.blackout = QCheckBox("启用断网测试")
        self.blackout.setToolTip("选择丢包模拟或系统禁用网卡，按指定时间恢复。")
        advanced_layout.addWidget(self.blackout)
        mode_row = QHBoxLayout()
        mode_row.addWidget(label("断网方式", "muted"))
        self.blackout_mode = combo([("丢包断网（网卡保持启用）", "packet"),
                                    ("网卡断网（系统禁用）", "adapter")])
        mode_row.addWidget(self.blackout_mode, 1)
        advanced_layout.addLayout(mode_row)
        self.adapter_row = QWidget()
        adapter_layout = QHBoxLayout(self.adapter_row)
        adapter_layout.setContentsMargins(0, 0, 0, 0)
        self.adapter_label = label("尚未选择网卡", "muted")
        self.adapter_label.setWordWrap(True)
        adapter_layout.addWidget(self.adapter_label, 1)
        self.adapter_choose = QPushButton("选择网卡")
        self.adapter_choose.clicked.connect(self.choose_adapters)
        adapter_layout.addWidget(self.adapter_choose)
        advanced_layout.addWidget(self.adapter_row)
        timing_row = QHBoxLayout()
        timing_row.addWidget(label("延后断网", "muted"))
        self.blackout_delay = QSpinBox()
        self.blackout_delay.setRange(0, 86400)
        self.blackout_delay.setSuffix(" 秒")
        self.blackout_delay.setMaximumWidth(125)
        self.blackout_delay.setToolTip("每轮断网前保持网络正常的时间；单次执行时，0 表示立即断网。")
        timing_row.addWidget(self.blackout_delay)
        timing_row.addSpacing(14)
        timing_row.addWidget(label("断网持续", "muted"))
        self.blackout_duration = QSpinBox()
        self.blackout_duration.setRange(0, 86400)
        self.blackout_duration.setSuffix(" 秒")
        self.blackout_duration.setSpecialValueText("手动停止")
        self.blackout_duration.setMaximumWidth(125)
        self.blackout_duration.setToolTip("断网持续多久，结束后自动恢复网络；0 表示手动停止，仅在延后断网为0秒时有效。")
        timing_row.addWidget(self.blackout_duration)
        timing_row.addStretch(1)
        advanced_layout.addLayout(timing_row)
        self.blackout_loop = QCheckBox("循环执行")
        self.blackout_loop.setToolTip("重复正常联网 → 断网 → 恢复网络，直到点击“停止并恢复”。两段时间均需至少 1 秒。")
        advanced_layout.addWidget(self.blackout_loop)
        self.blackout_hint = label("", "muted")
        self.blackout_hint.setWordWrap(True)
        advanced_layout.addWidget(self.blackout_hint)
        self.reorder_ms = QDoubleSpinBox()
        self.reorder_ms.setRange(1, 15000)
        self.reorder_ms.setSuffix(" ms")
        self.reorder_ms.setDecimals(0)
        reorder_row = QGridLayout()
        for col, (title, field) in enumerate([("重复包概率", self.numbers["duplicate_pct"]),
                                             ("乱序触发概率", self.numbers["reorder_pct"]),
                                             ("乱序暂存", self.reorder_ms)]):
            reorder_row.addWidget(label(title, "muted"), 0, col)
            reorder_row.addWidget(field, 1, col)
            reorder_row.setColumnStretch(col, 1)
        advanced_layout.addLayout(reorder_row)
        impairment_layout.addWidget(self.advanced_panel)
        self.advanced_panel.hide()
        duration_row = QHBoxLayout()
        duration_row.addWidget(label("自动恢复", "muted"))
        self.duration = QSpinBox()
        self.duration.setRange(0, 86400)
        self.duration.setSingleStep(10)
        self.duration.setSuffix(" 秒")
        self.duration.setSpecialValueText("手动停止")
        self.duration.setMaximumWidth(145)
        duration_row.addWidget(self.duration)
        duration_note = label("普通测试使用此时长；断网请展开高级选项", "muted")
        duration_note.setWordWrap(True)
        duration_row.addWidget(duration_note, 1)
        impairment_layout.addLayout(duration_row)
        self.name_field.textChanged.connect(self.update_selected_scene_name)
        settings_layout.addWidget(impairment)
        body.addWidget(self.settings)
        save_row = QHBoxLayout()
        self.save_button = QPushButton("保存场景")
        self.save_button.clicked.connect(self.save_config)
        save_row.addWidget(self.save_button)
        save_row.addStretch(1)
        self.save_as_button = QPushButton("另存为新场景")
        self.save_as_button.setObjectName("primary")
        self.save_as_button.clicked.connect(lambda: self.save_as_scenario())
        save_row.addWidget(self.save_as_button)
        body.addLayout(save_row)
        body.addStretch(1)
        self.scroll.setWidget(content)
        self.views.addWidget(self.scroll)

        record_page = QWidget()
        records = QVBoxLayout(record_page)
        records.setContentsMargins(0, 22, 0, 0)
        records.setSpacing(14)
        records.addWidget(label("测试记录", "section"))
        stats_row = QHBoxLayout()
        self.stats = {}
        for key, title in [("received", "已捕获 / 包"), ("sent", "已转发 / 包"),
                           ("dropped", "已丢弃 / 包"), ("queued", "等待发送 / 包")]:
            card, layout = panel(None)
            layout.addWidget(label(title, "muted"))
            self.stats[key] = label("0", "number")
            layout.addWidget(self.stats[key])
            stats_row.addWidget(card)
        records.addLayout(stats_row)
        logs, log_layout = panel(None)
        log_bar = QHBoxLayout()
        log_bar.addWidget(label("运行记录", "section"), 1)
        self.detail_stats = label("", "muted")
        self.detail_stats.setWordWrap(True)
        log_bar.addWidget(self.detail_stats)
        self.export_log_button = QPushButton("导出测试记录")
        self.export_log_button.clicked.connect(self.export_report)
        log_bar.addWidget(self.export_log_button)
        log_layout.addLayout(log_bar)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(124)
        log_layout.addWidget(self.log_view)
        records.addWidget(logs)
        self.record_hotkey = label("", "muted")
        records.addWidget(self.record_hotkey)
        records.addStretch(1)
        self.views.addWidget(record_page)
        flow_page = QWidget()
        flow = QVBoxLayout(flow_page)
        flow.setContentsMargins(0, 8, 0, 0)
        flow.setSpacing(10)
        flow_header = QHBoxLayout()
        flow_header.addWidget(label("弱网测试任务", "section"), 1)
        self.case_state = label("待开始", "badge")
        flow_header.addWidget(self.case_state)
        flow.addLayout(flow_header)
        mode_row = QHBoxLayout()
        mode_row.addWidget(label("测试方式", "muted"))
        self.case_mode = combo([("按业务场景", "plan"), ("按用例模板", "template")])
        mode_row.addWidget(self.case_mode, 1)
        mode_row.addWidget(label("测试级别", "muted"))
        self.case_level = combo([("冒烟", "smoke"), ("标准", "standard"),
                                 ("完整", "full"), ("自定义", "custom")])
        mode_row.addWidget(self.case_level, 1)
        flow.addLayout(mode_row)
        self.case_business_row = QWidget()
        business_row = QHBoxLayout(self.case_business_row)
        business_row.setContentsMargins(0, 0, 0, 0)
        business_row.addWidget(label("业务场景", "muted"))
        self.case_business = QComboBox()
        self.case_business.setMinimumWidth(180)
        business_row.addWidget(self.case_business, 1)
        self.case_business_new = QPushButton("新建")
        self.case_business_new.setToolTip("根据当前场景另存业务步骤和适用网络条件")
        self.case_business_new.clicked.connect(lambda: self.edit_business_scene(duplicate=True))
        business_row.addWidget(self.case_business_new)
        self.case_business_edit = QPushButton("编辑")
        self.case_business_edit.clicked.connect(self.edit_business_scene)
        business_row.addWidget(self.case_business_edit)
        self.case_business_import = QPushButton("导入")
        self.case_business_import.clicked.connect(self.import_business_scene)
        business_row.addWidget(self.case_business_import)
        flow.addWidget(self.case_business_row)
        self.case_level_row = QWidget()
        level_row = QHBoxLayout(self.case_level_row)
        level_row.setContentsMargins(0, 0, 0, 0)
        level_row.addWidget(label("网络条件", "muted"))
        self.case_condition_button = QPushButton("选择条件")
        self.case_condition_menu = QMenu(self)
        for condition in network_conditions():
            action = self.case_condition_menu.addAction(condition["title"])
            action.setCheckable(True)
            action.setData(condition["id"])
            action.setChecked(condition["id"] in {"CONN-01", "WEAK-01", "CONN-03"})
            action.toggled.connect(self.refresh_business_plan_preview)
        self.case_condition_button.setMenu(self.case_condition_menu)
        level_row.addWidget(self.case_condition_button)
        self.case_plan_hint = label("", "muted")
        level_row.addWidget(self.case_plan_hint, 1)
        flow.addWidget(self.case_level_row)
        self.case_template_row = QWidget()
        template_row = QHBoxLayout(self.case_template_row)
        template_row.setContentsMargins(0, 0, 0, 0)
        template_row.addWidget(label("测试模板", "muted"))
        self.case_template = QComboBox()
        self.case_template.setMinimumWidth(300)
        self.case_template.setToolTip("模板只定义测试流程；目标 IP、端口和过滤条件沿用左侧设置")
        self.case_template.currentIndexChanged.connect(self.select_case_template)
        template_row.addWidget(self.case_template, 1)
        self.case_template_import = QPushButton("导入模板")
        self.case_template_import.clicked.connect(self.import_test_template)
        template_row.addWidget(self.case_template_import)
        self.case_template_export = QPushButton("导出模板")
        self.case_template_export.clicked.connect(self.export_test_template)
        template_row.addWidget(self.case_template_export)
        flow.addWidget(self.case_template_row)
        system_row = QHBoxLayout()
        system_row.addWidget(label("测试对象", "muted"))
        self.case_system = QLineEdit()
        self.case_system.setPlaceholderText("填写系统或功能名称，写入本轮报告")
        self.case_system.setToolTip("例如：登录、商城、战斗、邮件；不要填写账号或敏感数据")
        system_row.addWidget(self.case_system, 1)
        flow.addLayout(system_row)
        self.case_progress = label("", "condition_title")
        flow.addWidget(self.case_progress)
        self.case_target = label("", "muted")
        self.case_target.setWordWrap(True)
        flow.addWidget(self.case_target)
        flow_content = QHBoxLayout()
        flow_content.setSpacing(14)
        self.case_list = QListWidget()
        self.case_list.setObjectName("case_list")
        self.case_list.setFixedWidth(218)
        self.case_list.setWordWrap(True)
        self.case_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        for case in self.case_preview:
            self.case_list.addItem(f"{case['id']}   {case['title']}   ·   待测")
        self.case_list.currentRowChanged.connect(self.select_case_for_review)
        flow_content.addWidget(self.case_list)
        detail, details = panel(None)
        self.case_title = label("", "section")
        self.case_title.setWordWrap(True)
        details.addWidget(self.case_title)
        self.case_profile = label("", "muted")
        self.case_profile.setWordWrap(True)
        details.addWidget(self.case_profile)
        details.addWidget(label("操作", "condition_title"))
        self.case_steps = label("")
        self.case_steps.setWordWrap(True)
        details.addWidget(self.case_steps)
        details.addWidget(label("预期结果", "condition_title"))
        self.case_expected = label("")
        self.case_expected.setWordWrap(True)
        details.addWidget(self.case_expected)
        details.addWidget(label("实际现象", "condition_title"))
        self.case_actual = QPlainTextEdit()
        self.case_actual.setPlaceholderText("记录重连提示、加载状态、操作结果或失败原因")
        self.case_actual.setFixedHeight(82)
        self.case_actual.textChanged.connect(self.remember_case_note)
        details.addWidget(self.case_actual)
        verdict = QHBoxLayout()
        self.case_pass = QPushButton("✓  通过")
        self.case_pass.setObjectName("pass")
        self.case_pass.clicked.connect(lambda: self.mark_case("通过"))
        verdict.addWidget(self.case_pass)
        self.case_fail = QPushButton("✕  不通过")
        self.case_fail.setObjectName("fail")
        self.case_fail.clicked.connect(lambda: self.mark_case("不通过"))
        verdict.addWidget(self.case_fail)
        details.addLayout(verdict)
        exception_verdict = QHBoxLayout()
        self.case_na = QPushButton("不适用")
        self.case_na.setToolTip("先在实际现象中填写不适用原因")
        self.case_na.clicked.connect(lambda: self.mark_case("不适用"))
        exception_verdict.addWidget(self.case_na)
        self.case_skip = QPushButton("跳过")
        self.case_skip.setToolTip("先在实际现象中填写本次未执行原因")
        self.case_skip.clicked.connect(lambda: self.mark_case("跳过"))
        exception_verdict.addWidget(self.case_skip)
        details.addLayout(exception_verdict)
        details.addStretch(1)
        flow_content.addWidget(detail, 1)
        flow.addLayout(flow_content, 1)
        flow_actions = QHBoxLayout()
        self.case_start = QPushButton("▶  开始测试")
        self.case_start.setObjectName("start")
        self.case_start.clicked.connect(self.start_case_session)
        flow_actions.addWidget(self.case_start)
        self.case_stop = QPushButton("■  停止并恢复")
        self.case_stop.setObjectName("stop")
        self.case_stop.clicked.connect(self.stop_case_flow)
        flow_actions.addWidget(self.case_stop)
        flow_actions.addStretch(1)
        self.case_export = QPushButton("导出结果")
        self.case_export.clicked.connect(self.export_case_report)
        flow_actions.addWidget(self.case_export)
        self.case_result = label("", "muted")
        self.case_result.setWordWrap(True)
        flow.addWidget(self.case_result)
        admin_row = QHBoxLayout()
        self.case_admin_label = label("", "admin_status")
        admin_row.addWidget(self.case_admin_label, 1)
        self.case_admin_button = QPushButton("以管理员身份重启")
        self.case_admin_button.clicked.connect(self.request_admin)
        admin_row.addWidget(self.case_admin_button)
        flow_scroll = QScrollArea()
        flow_scroll.setWidgetResizable(True)
        flow_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        flow_scroll.setWidget(flow_page)
        self.case_flow_scroll = flow_scroll
        flow_view = QWidget()
        flow_view_layout = QVBoxLayout(flow_view)
        flow_view_layout.setContentsMargins(0, 0, 0, 0)
        flow_view_layout.setSpacing(8)
        flow_view_layout.addWidget(flow_scroll, 1)
        flow_view_layout.addLayout(flow_actions)
        flow_view_layout.addLayout(admin_row)
        self.views.addWidget(flow_view)
        self.case_list.setCurrentRow(0)
        self.render_case_flow()
        self.case_mode.currentIndexChanged.connect(self.refresh_business_plan_preview)
        self.case_business.currentIndexChanged.connect(self.refresh_business_plan_preview)
        self.case_level.currentIndexChanged.connect(self.refresh_business_plan_preview)
        self.case_system.textChanged.connect(self.refresh_business_plan_preview)
        for field in (self.scope, self.protocol, self.direction):
            field.currentIndexChanged.connect(self.refresh_business_plan_preview)
        for field in (self.host, self.ports, self.advanced):
            field.textChanged.connect(self.refresh_business_plan_preview)
        right.addWidget(self.views, 1)
        outer.addLayout(right, 1)
        self.show_view(0)
        self.scope.currentIndexChanged.connect(self.update_scope)
        self.direction.currentIndexChanged.connect(self.sync_quick_direction)
        for c in (self.protocol, self.direction):
            c.currentIndexChanged.connect(self.update_preview)
        for field in (self.host, self.ports, self.advanced):
            field.textChanged.connect(self.update_preview)
        self.blackout.toggled.connect(self.update_blackout)
        self.blackout_mode.currentIndexChanged.connect(self.update_blackout)
        self.blackout_mode.currentIndexChanged.connect(self.scene_form_changed)
        self.blackout_loop.toggled.connect(self.update_blackout)
        self.blackout_delay.valueChanged.connect(self.update_blackout)
        self.blackout_duration.valueChanged.connect(self.update_blackout)
        if hasattr(self, "scene_form_changed"):
            for field in (*self.numbers.values(), self.duration, self.blackout_delay,
                          self.blackout_duration, self.reorder_ms):
                field.valueChanged.connect(self.scene_form_changed)
            for field in (self.scope, self.protocol, self.direction):
                field.currentIndexChanged.connect(self.scene_form_changed)
            for field in (self.host, self.ports, self.advanced):
                field.textChanged.connect(self.scene_form_changed)
            self.blackout.toggled.connect(self.scene_form_changed)
            self.blackout_loop.toggled.connect(self.scene_form_changed)

    def collect(self):
        data = dict(DEFAULT_CONFIG)
        data.update(name=self.name_field.text().strip(), scope=self.scope.currentData(),
                    protocol=self.protocol.currentData(), direction=self.direction.currentData(),
                    host=self.host.text().strip(), ports=self.ports.text().strip(),
                    advanced_filter=self.advanced.text().strip(), blackout=self.blackout.isChecked(),
                    start_delay_s=self.blackout_delay.value(), blackout_duration_s=self.blackout_duration.value(),
                    blackout_loop=self.blackout_loop.isChecked(),
                    blackout_mode=self.blackout_mode.currentData(), adapter_ids=list(self.adapter_ids),
                    duration_s=self.duration.value(), reorder_ms=self.reorder_ms.value())
        data.update({key: field.value() for key, field in self.numbers.items()})
        if data["scope"] not in ("endpoint", "web"):
            data.update(host="", ports="")
        if data["blackout"]:
            # In blackout mode the explicit duration beside the checkbox is the
            # authoritative recovery timer; the ordinary timer is disabled.
            data["duration_s"] = data["blackout_duration_s"]
        return data

    def load_form(self, data):
        self.loading = True
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        self.name_field.setText(merged["name"])
        self.name_field.setCursorPosition(0)
        for key, field in [("scope", self.scope), ("protocol", self.protocol), ("direction", self.direction)]:
            field.setCurrentIndex(max(0, field.findData(merged[key])))
        for key, field in [("host", self.host), ("ports", self.ports), ("advanced_filter", self.advanced)]:
            field.setText(merged[key])
        for key, field in self.numbers.items():
            field.setValue(merged[key])
        self.blackout.setChecked(merged["blackout"])
        self.blackout_mode.setCurrentIndex(self.blackout_mode.findData(merged.get("blackout_mode", "packet")))
        self.adapter_ids = list(merged.get("adapter_ids", []))
        self.refresh_adapter_label()
        self.blackout_duration.setRange(1, 300) if merged["blackout"] and merged.get("blackout_mode") == "adapter" else self.blackout_duration.setRange(0, 86400)
        self.blackout_delay.setValue(merged.get("start_delay_s", 0))
        self.blackout_duration.setValue(merged.get("blackout_duration_s", 10))
        self.blackout_loop.setChecked(merged.get("blackout_loop", False))
        self.duration.setValue(merged["duration_s"])
        self.reorder_ms.setValue(merged["reorder_ms"])
        self.loading = False
        self.update_scope()
        self.update_blackout()
        if hasattr(self, "sync_advanced_section"):
            self.sync_advanced_section()
        self.scene_form_changed()
        self.sync_quick_direction()
        self.sync_quick_scene()

    def update_scope(self, *_):
        if self.loading:
            return
        scope = self.scope.currentData()
        self.host.setPlaceholderText("游戏服务器域名或 URL，例如 https://game.example.com" if scope == "web" else
                                     "服务器 IP，例如 192.168.1.20")
        self.ports.setPlaceholderText("留空使用 URL 端口；HTTP/WS 80，HTTPS/WSS 443" if scope == "web" else "端口，例如 443, 8080")
        self.host.setVisible(scope in ("endpoint", "web"))
        self.ports.setVisible(scope in ("endpoint", "web"))
        self.advanced.setVisible(scope == "advanced")
        self.protocol.setEnabled(scope != "advanced")
        self.direction.setEnabled(scope not in ("advanced", "loopback"))
        self.direction.setItemText(self.direction.findData("outbound"),
                                  "全部回环流量" if scope == "loopback" else "仅上行（发送）")
        if scope == "loopback":
            self.direction.setCurrentIndex(self.direction.findData("outbound"))
        notes = {
            "all": "作用于整台电脑的匹配流量。可切换“指定 IP / 端口”，集中测试游戏服务器连接。",
            "endpoint": "填写服务器 IP 和端口后只影响该目标。网页不能直接填 URL：先将域名解析成 IP；HTTPS 常用端口 443，HTTP 常用端口 80。多个端口用英文逗号分隔。",
            "web": "只处理目标 IP 和端口，支持丢包循环断网；不会禁用网卡或触发系统断网事件。localhost 通常只是页面入口，游戏服 / WebSocket 可能是另一地址。共享 IP 的其他服务也可能受影响；每次开始时重新解析域名。",
            "loopback": "处理全部本机回环数据包，不区分业务上传和下载。测试单向流量请选择“指定网页 / 域名”并填写本地服务地址和端口。",
            "advanced": "以表达式定义全部匹配条件；上方协议和方向在此模式下不参与过滤。",
        }
        self.scope_note.setText(notes[scope])
        self.sync_quick_direction()
        self.update_preview()

    def update_blackout(self, *_):
        if self.loading:
            return
        adapter = self.blackout.isChecked() and self.blackout_mode.currentData() == "adapter"
        self.blackout_mode.setEnabled(self.blackout.isChecked())
        self.adapter_row.setVisible(adapter)
        self.packet_target.setEnabled(not adapter)
        self.quick_up.setEnabled(not adapter and not self.session_active)
        self.quick_down.setEnabled(not adapter and not self.session_active)
        if adapter:
            self.scope.setCurrentIndex(self.scope.findData("all"))
            self.protocol.setCurrentIndex(self.protocol.findData("all"))
            self.direction.setCurrentIndex(self.direction.findData("both"))
            self.host.clear()
            self.ports.clear()
            self.advanced.clear()
            self.blackout_loop.setChecked(False)
            self.blackout_duration.setRange(1, 300)
        else:
            self.blackout_duration.setRange(0, 86400)
        self.blackout_duration.setSpecialValueText("" if adapter else "手动停止")
        self.blackout_duration.setToolTip("网卡禁用 1–300 秒后自动重新启用；也可提前停止恢复。" if adapter else
                                        "0 表示手动停止，仅在延后断网为0秒时有效。")
        for field in self.numbers.values():
            field.setEnabled(not self.blackout.isChecked())
        self.reorder_ms.setEnabled(not self.blackout.isChecked())
        self.duration.setEnabled(not self.blackout.isChecked())
        self.blackout_delay.setEnabled(self.blackout.isChecked())
        self.blackout_duration.setEnabled(self.blackout.isChecked())
        self.blackout_loop.setEnabled(self.blackout.isChecked() and not adapter)
        if not self.blackout.isChecked():
            self.blackout_loop.setChecked(False)
        if adapter:
            self.blackout_hint.setText("禁用所选网卡，影响其上的所有应用；其他网卡仍可联网。"
                                       "到时或停止时重新启用，Wi-Fi 重新连接可能还需数秒。本版网卡断网单次执行。")
        elif self.blackout_loop.isChecked():
            self.blackout_hint.setText(
                f"循环：正常联网 {self.blackout_delay.value()} 秒 → 断网 {self.blackout_duration.value()} 秒 → 恢复后重复，直到停止。"
                "两段时间均需至少 1 秒。")
        else:
            self.blackout_hint.setText("丢弃匹配流量，网卡保持启用；不会触发系统网卡断开。延迟断网的持续时间至少为 1 秒。")
        self.update_preview()

    def refresh_adapter_label(self):
        if hasattr(self, "adapter_label"):
            names = [self.adapter_names.get(guid, guid) for guid in self.adapter_ids]
            self.adapter_label.setText("已选：" + "、".join(names) if names else "尚未选择网卡")

    def choose_adapters(self):
        from .adapter_ui import AdapterPicker
        dialog = AdapterPicker(self, self.adapter_ids)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.chosen()
            self.adapter_ids = [row["guid"] for row in selected]
            self.adapter_names.update({row["guid"]: row["name"] for row in selected})
            self.refresh_adapter_label()
            self.scene_form_changed()

    def prepare_adapter_outage(self):
        if self.busy or self.session_active:
            return
        config = dict(DEFAULT_CONFIG, name="网卡断网", blackout=True, blackout_mode="adapter",
                      adapter_ids=list(self.adapter_ids), start_delay_s=3, blackout_duration_s=5,
                      duration_s=5, scope="all", protocol="all", direction="both",
                      delay_ms=0, jitter_ms=0, loss_pct=0, bandwidth_kbps=0)
        self.selected_scenario_id = None
        self.selected_builtin_index = None
        self.presets.blockSignals(True)
        self.presets.setCurrentRow(-1)
        self.presets.blockSignals(False)
        self.load_form(config)
        self.show_view(1)
        self.choose_adapters()
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.blackout_hint, 0, 20))

    def prepare_web_outage(self):
        if self.busy or self.session_active:
            return
        previous = self.collect()
        config = dict(DEFAULT_CONFIG, name="网页循环断网", scope="web", blackout=True,
                      blackout_mode="packet", blackout_loop=True, start_delay_s=10,
                      blackout_duration_s=5, duration_s=5, delay_ms=0, jitter_ms=0, loss_pct=0,
                      host=previous["host"] if previous["scope"] == "web" else "",
                      ports=previous["ports"] if previous["scope"] == "web" else "")
        self.selected_scenario_id = self.selected_builtin_index = None
        self.presets.blockSignals(True)
        self.presets.setCurrentRow(-1)
        self.presets.blockSignals(False)
        self.load_form(config)
        self.show_view(1)
        self.host.setFocus()
        QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(self.packet_target, 0, 20))

    def update_preview(self, *_):
        if self.loading:
            return
        try:
            data = self.collect()
            if data["scope"] == "web":
                cfg = validate_config(data)
                self.scope_note.setText(self.web_scope_note(cfg))
                if self.is_current_scene_running() and self.engine.snapshot().get("web_target"):
                    self.filter_preview.setText(self.engine.snapshot()["web_target"]["filter"])
                else:
                    self.filter_preview.setText(f"{self.packet_direction(cfg)} · 开始时解析 {cfg['host']} · 端口 {cfg['ports']}")
                return
            self.filter_preview.setText("网卡断网作用于所选网卡的全部流量，不使用 IP / 端口过滤。" if
                                       data["blackout"] and data.get("blackout_mode") == "adapter"
                                       else build_filter(data))
        except (ValueError, TypeError) as exc:
            if self.scope.currentData() == "web":
                self.scope_note.setText("填写实际游戏服 / WebSocket 地址；页面入口不会自动覆盖它连接的其他服务器。")
            self.filter_preview.setText(f"待完善：{exc}")

    def refresh_privilege(self):
        admin = is_admin()
        if admin:
            self.admin_label.setText("●  管理员权限已就绪，可开始真实测试")
            self.admin_label.setToolTip("当前已获得管理员权限，WinDivert 可以拦截和恢复网络流量。")
        else:
            self.admin_label.setText("⚠  请先以管理员身份重启后使用")
            self.admin_label.setToolTip("点击右侧“以管理员身份重启”，在系统弹窗中选择“是”，然后使用新窗口开始真实测试。")
        self.admin_label.setProperty("ready", admin)
        self.admin_label.style().unpolish(self.admin_label)
        self.admin_label.style().polish(self.admin_label)
        self.admin_button.setVisible(not admin)
        self.case_admin_label.setText("管理员权限已就绪" if admin else "请先以管理员身份重启后使用")
        self.case_admin_label.setProperty("ready", admin)
        self.case_admin_label.style().unpolish(self.case_admin_label)
        self.case_admin_label.style().polish(self.case_admin_label)
        self.case_admin_button.setVisible(not admin)
        self.start_button.setToolTip("开始拦截并处理匹配流量" if admin else "请先以管理员身份重启后使用，再开始真实网络测试")

    def append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"{timestamp}  {message}")

    def record(self, event, **details):
        try:
            row = self.audit.write(event, **details)
            self.session_log.append(row)
        except OSError as exc:
            self.append_log(f"操作记录写入失败：{exc}")

    def error(self, message, title="操作未完成"):
        self.append_log(message)
        # Use an opaque themed dialog, including on Windows native palettes.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(str(title))
        box.setText(str(message))
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setStyleSheet(stylesheet(self.theme_id))
        box.exec()

    def request_admin(self):
        try:
            self.store.save(validate_config(self.collect()))
            # Release the same-user instance lock before the elevated process starts.
            if self.instance_lock:
                self.instance_lock.release()
            try:
                elevate()
            except Exception:
                if self.instance_lock:
                    self.instance_lock.acquire()
                raise
            self.adapter_recovery_handed_off = self.needs_adapter_recovery() and not is_admin()
            self.close()
        except (ValueError, OSError) as exc:
            self.error(
                "管理员重启未完成。请点击“确定”关闭提示，然后点击“以管理员身份重启”；\n"
                "在 Windows 安全提示中选择“是”，再使用新打开的 Motu Net。\n\n"
                f"详细信息：{exc}",
                title="需要管理员权限",
            )

    def job(self, name, operation):
        self.busy = True
        self.current_job = name
        self.set_running_controls()
        def run():
            try:
                operation()
                result = None
            except Exception as exc:
                result = str(exc)
            self.jobs.done.emit(name, result)
        threading.Thread(target=run, daemon=True, name=f"NetLab-{name}").start()

    def start_test(self):
        if self.case_run and self.case_run["status"] == "进行中":
            return
        self._start_config(self.collect(), persist=True)

    def _start_config(self, candidate, *, persist, require_admin=True):
        if self.busy or self.update_busy:
            return False
        if self.needs_adapter_recovery() and self.engine.snapshot().get("phase") == "restore_failed":
            self.error("网卡恢复尚未确认，请先点击“重试恢复”，再启动其他测试。")
            return False
        if persist and self.is_current_scene_running():
            self.set_running_controls()
            return False
        if require_admin and not is_admin():
            self.error(
                "请先以管理员身份重启后使用。\n\n"
                "点击“以管理员身份重启”，在 Windows 安全提示中选择“是”，"
                "然后在新打开的窗口中点击“开始测试”。",
                title="需要管理员权限",
            )
            return False
        try:
            config = validate_config(candidate)
            if config["blackout"] and config.get("blackout_mode") == "adapter":
                if not config.get("adapter_ids"):
                    raise ValueError("请先在详细设置中选择要禁用的网卡")
            else:
                self.engine.validate_filter(config["filter"])
            if persist:
                # Web addresses are resolved by Engine.start, never persisted
                # as stale DNS results from the previous session.
                self.store.save(dict(config, web_ips=[]))
            # Starting is refused if its persistent audit record cannot be written.
            row = self.audit.write("test_start_requested", config=config)
            self.session_log = [row]
        except (OSError, ValueError, RuntimeError) as exc:
            self.error(str(exc))
            return False
        self.last_error = ""
        if self.session_active or self.engine.snapshot().get("running"):
            self.cancel_switch = False
            self.pending_switch_config = config
            self.status.setText("●  正在切换场景")
            self.run_banner.setText(f"正在切换到「{config['name']}」：先恢复当前网络，再启动新场景…")
            self.job("switch_stop", self.engine.stop)
        else:
            self.pending_switch_config = None
            self.cancel_switch = False
            self.session_config = config
            self.status.setText("●  正在启动")
            self.job("start", lambda: self.engine.start(config))
        return True

    def stop_test(self):
        if self.case_run and self.case_run["status"] == "进行中" and self.case_pending_result is None:
            self.abort_case_run()
        if self.busy:
            self.stop_pending = True
            if self.current_job in ("switch_stop", "switch_start"):
                self.cancel_switch = True
            return
        if self.session_active or self.engine.snapshot().get("running"):
            self.status.setText("●  正在恢复")
            self.job("stop", self.engine.stop)

    def job_done(self, name, error):
        self.busy = False
        self.current_job = None
        if error:
            if name == "update_stop":
                self.update_busy = False
                self.update_button.setEnabled(True)
            self.session_active = bool(self.engine.snapshot().get("running"))
            # A failed adapter restore must remain stoppable, with no automatic
            # close/retry loop or transition to another impairment.
            self.close_pending = False
            self.stop_pending = False
            self.pending_switch_config = None
            self.record("test_error", operation=name, error=error)
            self.status.setText("●  启动失败" if name in ("start", "switch_start") else "●  恢复失败")
            self.run_banner.setText("恢复尚未确认，请点击“重试恢复”；独立恢复任务仍会继续尝试。" if self.session_active else
                                    "操作失败，请检查日志。")
            self.error(error)
        elif name in ("start", "switch_start"):
            self.session_active = True
            if self.session_config.get("scope") == "web":
                resolved = self.engine.snapshot().get("web_target")
                if resolved:
                    self.session_config.update(resolved)
                    self.append_log(f"网页目标 {resolved['host']} · 端口 {resolved['ports']} · 本次 IP：{'、'.join(resolved['web_ips'])}")
            self.record("test_started", config=self.session_config)
            target = ("系统禁用网卡：" + "、".join(self.session_config.get("adapter_ids", []))
                      if self.session_config.get("blackout_mode") == "adapter" and self.session_config.get("blackout")
                      else "过滤条件：" + self.session_config["filter"])
            self.append_log(f"开始「{self.session_config['name']}」；{target}")
            self.run_banner.setText(f"正在运行：「{self.session_config['name']}」")
            self.update_preview()
        elif name == "update_stop":
            self.stop_pending = False
            self.finish_session("准备更新软件")
            self.begin_update_download()
            return
        elif name == "switch_stop":
            if self.cancel_switch or self.close_pending or self.stop_pending:
                self.pending_switch_config = None
                self.cancel_switch = False
                self.session_active = False
                self.finish_session("切换已取消")
            else:
                config = self.pending_switch_config
                self.session_config = config
                self.status.setText("●  正在启动")
                self.job("switch_start", lambda: self.engine.start(config))
                return
        else:
            self.finish_session("手动停止")
        self.set_running_controls()
        self.case_job_done(name, error)
        if self.stop_pending:
            self.stop_pending = False
            if self.session_active:
                self.stop_test()
                return
            self.stop_pending = False
        if self.close_pending:
            if self.session_active:
                self.stop_test()
            else:
                self.close()

    def set_running_controls(self):
        flow_active = bool(self.case_run and self.case_run["status"] == "进行中")
        editing = not self.busy and not self.session_active and not self.update_busy and not flow_active
        snapshot = self.engine.snapshot()
        recovering = snapshot.get("phase") in ("restoring", "restore_failed", "disabling")
        adapter_mode = self.blackout.isChecked() and self.blackout_mode.currentData() == "adapter"
        self.settings.setEnabled(editing)
        self.adapter_shortcut.setEnabled(editing)
        self.web_shortcut.setEnabled(editing)
        # Presets remain selectable while a session runs; selection only edits
        # the candidate form and never changes the active engine configuration.
        self.presets.setEnabled(not self.busy and not self.update_busy and not flow_active)
        self.quick_scene.setEnabled(not self.busy and not self.update_busy and not flow_active)
        for button in self.scene_cards.values():
            button.setEnabled(not self.busy and not self.update_busy and not flow_active)
        self.quick_up.setEnabled(editing and self.direction.isEnabled() and not adapter_mode)
        self.quick_down.setEnabled(editing and self.direction.isEnabled() and not adapter_mode)
        self.import_button.setEnabled(editing)
        self.export_button.setEnabled(editing)
        self.admin_button.setEnabled(editing or (not is_admin() and self.needs_adapter_recovery() and not self.busy))
        self.name_field.setReadOnly(not editing)
        self.new_button.setEnabled(editing)
        saved_scene = any(row["id"] == getattr(self, "selected_scenario_id", None)
                          for row in getattr(self, "user_scenarios", []))
        self.delete_button.setEnabled(editing and saved_scene)
        self.delete_button.setToolTip(
            "删除选中的自定义场景，删除前需确认" if editing and saved_scene else
            "请先停止测试并等待当前操作完成" if not editing else
            "选择已保存的自定义场景后可删除；内置场景保留")
        self.save_button.setEnabled(editing)
        self.save_as_button.setEnabled(not self.busy and not self.update_busy and not flow_active)
        same_scene = self.is_current_scene_running()
        self.start_button.setEnabled(not flow_active and (editing or (self.session_active and not self.busy)) and not same_scene and not self.update_busy and not recovering)
        self.start_button.setText("当前场景运行中" if same_scene else ("↻  切换并启动" if self.session_active else "▶  开始测试"))
        if self.session_active:
            self.start_button.setToolTip(("当前网卡断网已启动，请查看顶部网卡状态" if adapter_mode else
                                         "当前参数已启动，请查看实际捕获和丢弃计数") if same_scene else
                                        "先恢复当前网络，再启动左侧选中的新场景")
        else:
            self.start_button.setToolTip("开始测试；首次使用请先以管理员身份重启")
        recovery_requires_admin = snapshot.get("recovery_pending") and not is_admin()
        self.stop_button.setEnabled(self.session_active and not self.busy and not recovery_requires_admin)
        self.stop_button.setText("重试恢复" if snapshot.get("phase") == "restore_failed" else "■  停止并恢复")
        self.stop_button.setVisible(self.session_active or self.busy)
        self.run_info.setVisible(self.session_active or self.busy)
        self.refresh_theme_state()
        self.render_case_flow()

    def finish_session(self, reason):
        self.session_active = False
        snapshot = self.engine.snapshot()
        self.case_session_finished(snapshot, reason)
        self.record("test_stopped", reason=reason, stats=snapshot)
        adapter = bool(self.session_config and self.session_config.get("blackout_mode") == "adapter" and self.session_config.get("blackout"))
        attempted = snapshot.get("adapter_attempted", True)
        summary = ("已取消断网，所选网卡保持启用。" if adapter and not attempted else
                   "所选网卡已重新启用；应用重连与 Wi-Fi 连接状态请继续观察。" if adapter and snapshot.get("adapter_restored") else
                   "网卡未被禁用。" if adapter else
                   f"已释放拦截句柄。捕获 {snapshot.get('received', 0)} 包，丢弃 {snapshot.get('dropped', 0)} 包。")
        self.append_log(f"测试结束 · {reason}；{summary}")
        self.status.setText("●  已停止")
        self.run_banner.setText("当前未运行弱网场景")
        self.run_note.setText(summary if adapter else "已停止拦截。应用连接是否恢复可继续在游戏中验收。")
        if adapter:
            self.status.setText("●  网卡已启用" if snapshot.get("adapter_restored") else "●  已停止")
        self.set_running_controls()

    def tick(self):
        snapshot = self.engine.snapshot()
        for key, field in self.stats.items():
            field.setText(f"{snapshot.get(key, 0):,}")
        self.detail_stats.setText(
            f"随机丢弃 {snapshot.get('loss_dropped', 0)} · 断网丢弃 {snapshot.get('blackout_dropped', 0)} · "
            f"队列满丢弃 {snapshot.get('overflow_dropped', 0)} · "
            f"重复 {snapshot.get('duplicated', 0)} · 乱序 {snapshot.get('reordered', 0)} · 错误 {snapshot.get('errors', 0)}")
        if self.case_run and self.case_run["status"] == "进行中":
            self.case_state.setText(f"第 {self.case_index + 1} / {len(self.case_run['cases'])} 项 · 捕获 {snapshot.get('received', 0)} 包")
        if self.session_active and not self.busy:
            if not snapshot.get("running"):
                reason = snapshot.get("error") or "自动恢复 / 引擎停止"
                self.finish_session(reason)
            else:
                # A blackout with a delayed start has two distinct phases.  Keep
                # the status text explicit so the operator can tell that traffic
                # is still flowing during the countdown and when the drop phase
                # is actually active.  ``ceil`` prevents displaying 0 seconds
                # while there is still a fraction of a second left.
                phase = snapshot.get("phase")
                name = self.session_config["name"]
                if self.session_config.get("blackout_mode") == "adapter" and self.session_config.get("blackout"):
                    names = "、".join(snapshot.get("adapter_names", [])) or "所选网卡"
                    if phase == "waiting":
                        detail = f"等待 {math.ceil(snapshot.get('wait_remaining', 0))} 秒后禁用 · {names}"
                        state = "等待网卡断开"
                    elif phase == "active":
                        detail = f"已确认禁用 · {names} · {math.ceil(snapshot.get('active_remaining') or 0)} 秒后重新启用"
                        state = "网卡已禁用"
                    elif phase == "disabling":
                        detail, state = f"正在禁用并检查状态 · {names}", "正在断开网卡"
                    elif phase == "restore_failed":
                        if not is_admin():
                            detail = "请先点击“以管理员身份重启”，新窗口将自动恢复上次网卡状态。"
                            state = "等待管理员恢复"
                        else:
                            detail = "恢复尚未确认，请点击“重试恢复”。" + str(snapshot.get("error") or "")
                            state = "网卡恢复失败"
                    else:
                        detail, state = f"正在重新启用并检查状态 · {names}", "正在恢复网卡"
                    self.status.setText("●  " + state)
                    self.run_banner.setText(f"正在运行：「{name}」 · 系统网卡断网\n{detail}")
                    self.run_note.setText("网卡模式不统计丢包；恢复表示重新启用网卡，应用重连可能稍后完成。")
                    self.set_running_controls()
                    return
                looping = self.session_config.get("blackout_loop", False)
                direction = self.packet_direction(self.session_config)
                cycle = f"循环 · 第 {snapshot.get('cycle', 1)} 轮 · " if looping else ""
                if phase == "waiting":
                    wait_remaining = max(0.0, float(snapshot.get("wait_remaining", 0)))
                    self.status.setText(f"●  {cycle}等待断网")
                    self.run_note.setText(
                        f"{name}  ·  {cycle}等待 {math.ceil(wait_remaining)} 秒后自动断网"
                    )
                    self.run_banner.setText(f"正在运行：「{name}」 · {direction}\n{cycle}网络正常 · 等待 {math.ceil(wait_remaining)} 秒后自动断网")
                    return

                elapsed = float(snapshot.get("elapsed", 0))
                if phase == "active" and self.session_config.get("blackout"):
                    active_remaining = snapshot.get("active_remaining")
                    self.status.setText(f"●  {cycle}断网进行中")
                    if active_remaining is None:
                        suffix = "手动停止"
                    else:
                        suffix = f"剩余 {math.ceil(max(0.0, float(active_remaining)))} 秒"
                    if looping:
                        suffix += "，恢复后继续下一轮"
                    self.run_note.setText(f"{name}  ·  {cycle}已断网 {int(snapshot.get('active_elapsed', 0))} 秒  ·  {suffix}")
                    evidence = traffic_feedback(self.session_config, snapshot)
                    self.run_banner.setText(f"正在运行：「{name}」 · {direction}断网\n{cycle}断网进行中 · {suffix}\n{evidence}")
                    return

                duration = self.session_config.get("duration_s", 0)
                remaining = max(0, duration - int(elapsed)) if duration else None
                suffix = f"{remaining} 秒后自动恢复" if remaining is not None else "手动停止"
                self.status.setText("●  测试进行中" if snapshot.get("received", 0) else "●  等待目标流量")
                self.run_note.setText(f"{name}  ·  已运行 {int(elapsed)} 秒  ·  {suffix}")
                evidence = traffic_feedback(self.session_config, snapshot)
                self.run_banner.setText(f"正在运行：「{name}」 · {direction} · {suffix}\n{evidence}")

    def import_config(self):
        scenario_dir = resource_dir() / "scenarios"
        start_dir = str(scenario_dir if scenario_dir.exists() else Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "导入场景配置", start_dir, "Motu Net 配置 (*.json)")
        if not path:
            return
        try:
            config = self.store.import_config(Path(path))
            self.selected_scenario_id = None
            self.selected_builtin_index = None
            self.load_form(config)
            self.presets.blockSignals(True)
            self.presets.setCurrentRow(-1)
            self.presets.blockSignals(False)
            self.record("config_imported", source=path)
            self.scene_form_changed()
            self.append_log(f"已导入「{config['name']}」，可保存到场景库；点击开始测试后生效。")
        except (OSError, ValueError) as exc:
            self.error(str(exc))

    def export_config(self):
        try:
            config = validate_config(self.collect())
            path, _ = QFileDialog.getSaveFileName(self, "导出场景配置", "MotuNet-scene.json", "Motu Net 配置 (*.json)")
            if path:
                self.store.export(Path(path), config)
                self.record("config_exported", destination=path)
                self.append_log(f"配置已导出：{path}")
        except (OSError, ValueError) as exc:
            self.error(str(exc))

    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出测试记录", f"MotuNet-{datetime.now():%Y%m%d-%H%M%S}.json", "JSON (*.json)")
        if not path:
            return
        report = {"app_version": __version__, "exported_at": datetime.now().astimezone().isoformat(),
                  "config": self.session_config, "stats": self.engine.snapshot(), "events": self.session_log,
                  "note": "计数来自 WinDivert 实际处理；仅保存参数与操作信息，不包含包内容。本地日志不具备防篡改能力。"}
        try:
            Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            self.append_log(f"测试记录已导出：{path}")
        except OSError as exc:
            self.error(str(exc))

    def show_help(self):
        """Open a high-contrast, searchable usage guide."""
        if self.help_dialog is not None and self.help_dialog.isVisible():
            self.help_dialog.raise_()
            self.help_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} · 使用说明")
        dialog.resize(760, 620)
        dialog.setStyleSheet(stylesheet(self.theme_id))
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml("""
        <h2>Motu Net · 使用说明</h2>
        <p>用于在 Windows 测试机上稳定复现延迟、丢包、带宽不足和断网等网络问题。修改参数后点击“开始测试”，结束时点击“停止并恢复”。</p>
        <h3>一、快速开始</h3>
        <ol><li>在“场景测试”页点击常用场景卡，或从“当前场景”下拉框选择内置及已保存场景。</li><li>需要指定服务器 IP / 端口或调整参数时，打开“详细设置”；场景导入、保存和管理也在这一页。</li><li>以管理员身份运行后，检查上传/下载方向，点击“开始测试”。延迟断网倒计时从此刻开始。</li><li>测试完成点击“停止并恢复”；计数和日志可在“测试记录”查看。等待断网或断网进行中都可以随时停止。</li></ol>
        <p>顶部显示实际运行场景。同一组参数已生效时，按钮显示“当前场景运行中”；选择另一场景后可点击“切换并启动”，先恢复网络再启动新场景。</p>
        <h3>二、流量目标</h3>
        <p><b>整台电脑</b>：处理除本机回环外的匹配流量。<br><b>指定 IP / 端口</b>：仅匹配目标地址或端口。<br><b>指定网页 / 域名</b>：输入实际游戏服务器域名或 http/https/ws/wss 地址，按启动时解析到的 IP 和端口执行弱网，不禁用网卡；其他 IP 和端口不匹配。HTTP/WS 默认80，HTTPS/WSS及裸域名默认443，也可手填端口。每次开始测试时重新解析，循环期间不动态刷新。本地 localhost 页面可能连接另一台游戏服务器，需要填写实际游戏服或 WebSocket 地址。一次只运行一个目标；URL 路径无法单独过滤，同 IP 和端口上的其他服务也会受影响。<br><b>本机回环</b>：测试 127.0.0.1 等本机服务。<br><b>高级过滤表达式</b>：直接编写 WinDivert 条件；会覆盖协议和方向选择。</p>
        <h3>三、网络条件参数</h3>
        <p><b>上传 / 下载</b>：网页目标的上传指客户端发往目标服务器，下载指目标服务器返回；localhost 也按目标服务端口区分。界面显示发送记录不等于服务器已收到。仅下行断网时，上行请求仍可能成功执行；TCP 确认包也会受单向丢包影响，另一方向不保证长期正常。</p>
        <p><b>延迟</b>：每个数据包增加的单程毫秒数。双向 150 ms 约增加 300 ms 往返时延。<br><b>延迟抖动 ±</b>：在延迟值附近随机增减，可能造成包顺序变化。<br><b>随机丢包</b>：按百分比随机丢弃数据包。<br><b>每方向带宽上限</b>：上下行分别限速，单位 KiB/s；0 表示不限速。<br><b>重复包概率</b>：命中后额外发送一份相同数据包。<br><b>乱序触发概率 / 暂存</b>：暂存命中的包，让后续包先发送。<br><b>丢包断网</b>：选择“丢包断网”并勾选后，点击“开始测试”即开始计时；“延后断网 N 秒”会先保持网络正常，倒计时结束后自动丢弃匹配流量，并持续“断网持续 M 秒”，随后自动恢复。等待或断网期间都可点击“停止并恢复”。<br><b>断网持续为0</b>：显示“手动停止”，仅在延后断网为0秒时有效。<br><b>普通测试自动恢复</b>：非断网模式下的倒计时；0 表示手动停止。</p>
        <p><b>循环执行</b>：仅在丢包断网方式下勾选，重复“正常联网 N 秒 → 断网 M 秒 → 恢复网络”，直到点击“停止并恢复”。两段时间均需至少 1 秒。顶部显示当前轮次和阶段倒计时，循环开关可随场景保存；默认关闭，执行一次。</p>
        <h3>系统网卡断网</h3>
        <p>在首页点击“网卡断网 · 系统禁用”，选择当前使用的以太网或 Wi-Fi，再设置延迟开始和持续时间。返回场景测试页点击“开始测试”。效果等同 Windows 高级网络设置中的“禁用”；影响所选网卡上的所有应用，不支持 IP、端口或单方向限制。多张网卡同时联网时应选择需要断开的全部连接。</p>
        <p>网卡断网单次持续 1–300 秒，到时自动重新启用，也可提前点击“停止并恢复”。后台独立恢复进程监控程序退出，并提前登记系统到时恢复任务。恢复失败会保留重试入口；重新启用不代表游戏已重连成功，Wi-Fi 重新连接也可能需要几秒。老场景继续采用丢包方式。</p>
        <h3>四、我的场景与皮肤</h3>
        <p><b>新建场景</b>：在“详细设置”的场景库新建，从正常网络开始创建，调整参数后点击“保存修改”。<br><b>另存为新场景</b>：复制当前全部目标和参数，原场景仍保留。<br><b>保存修改</b>：更新选中的自定义场景。内置场景始终保留，修改后请另存为。名称不可为空、与其他场景重复或超过80字。保存后可从首页“当前场景”选择，重启也会保留。<br><b>删除场景</b>：在详细设置选中已保存场景后点击“删除场景”并确认。删除后回到“正常对照”，不会启动测试；内置场景不可删除，测试或更新期间暂不可删除。<br><b>导入与导出</b>：用 JSON 文件与同事分享参数；导入后点击保存即可加入场景库。<br><b>皮肤</b>：左下角可切换蓝白、深色、青绿，自动记住选择，不影响正在运行的测试。</p>
        <h3>五、恢复与记录</h3>
        <p><b>检查更新</b>：启动后自动在后台检查 GitHub 最新版本，也可点击左下角“检查更新”。发现新版本后可选择“下载并更新”或“稍后”。确认更新会停止弱网测试、恢复网络，下载并校验安装包，然后打开安装向导。检查失败不影响正常使用；网络需能访问 GitHub。</p>
        <p>仅处理过滤条件匹配的数据包，不记录包内容。停止、自动恢复、关闭窗口和快捷键 Ctrl + Alt + F12 均会释放拦截句柄。可导出场景配置和测试记录供复盘。</p>
        <h3>六、如何确认丢包生效</h3>
        <p>参数保存后必须以管理员身份点击“开始测试”。顶部显示本轮实际捕获包数、随机丢弃包数和观测比例；未捕获流量时会提示检查目标。20% 表示每个匹配包独立以 20% 概率丢弃，少量样本不会严格等于 20%。队列满导致的额外丢弃单独统计。</p>
        <p>TCP 会自动重传，丢包不一定马上断线或弹出重连界面。停止或自动恢复后，客户端缓冲中的请求可能补发，游戏也可能重连后重试；应核对服务器在断网期间的收包时间，不能仅凭重登后收到奖励判定穿透。UDP 是否重试取决于应用。丢包断网不会关闭 Wi-Fi 网卡。本机服务的单向测试请用“指定网页 / 域名”填写本地地址和端口；“本机回环”处理全部回环数据包，“整台电脑”不含回环。</p>
        """)
        layout.addWidget(browser, 1)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.close)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.help_dialog = dialog
        dialog.finished.connect(lambda *_: setattr(self, "help_dialog", None))
        dialog.show()

    def show_changelog(self):
        """Show the packaged release notes so testers can see what changed."""
        if self.changelog_dialog is not None and self.changelog_dialog.isVisible():
            self.changelog_dialog.raise_()
            self.changelog_dialog.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{APP_NAME} · 更新日志")
        dialog.resize(680, 520)
        dialog.setStyleSheet(stylesheet(self.theme_id))
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser()
        changelog = resource_dir() / "CHANGELOG.md"
        try:
            browser.setPlainText(changelog.read_text(encoding="utf-8"))
        except OSError:
            browser.setPlainText("暂未找到更新日志文件。")
        layout.addWidget(browser, 1)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.close)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)
        self.changelog_dialog = dialog
        dialog.finished.connect(lambda *_: setattr(self, "changelog_dialog", None))
        dialog.show()

    def nativeEvent(self, event_type, message):
        if os.name == "nt":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0312 and msg.wParam == 0x4e4c:
                self.stop_test()
                return True, 0
        return super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        if self.case_run and self.case_run["status"] == "进行中":
            self.abort_case_run("关闭窗口")
        if self.update_busy:
            self.close_pending = True
            self.update_cancel.set()
            event.ignore()
            return
        if (self.busy or self.session_active) and not getattr(self, "adapter_recovery_handed_off", False):
            self.close_pending = True
            self.stop_test()
            event.ignore()
            return
        if self.engine.snapshot().get("running") and not getattr(self, "adapter_recovery_handed_off", False):
            self.close_pending = True
            self.job("stop", self.engine.stop)
            event.ignore()
            return
        if self.hotkey_registered:
            ctypes.windll.user32.UnregisterHotKey(wintypes.HWND(int(self.winId())), 0x4e4c)
            self.hotkey_registered = False
        self.record("app_close")
        self.close_updates()
        event.accept()
