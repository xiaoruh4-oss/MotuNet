"""Chinese desktop workflow for repeatable network impairment sessions."""
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
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
    QScrollArea, QSizePolicy, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from . import APP_NAME, APP_SUBTITLE, __version__
from .config import DEFAULT_CONFIG, PRESETS, build_filter, validate_config
from .engine import Engine
from .platform_win import elevate, is_admin, resource_dir
from .storage import AuditLog, ConfigStore
from .themes import THEMES, stylesheet
from .scene_workflow import SceneWorkflow
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


class MainWindow(UpdateWorkflow, SceneWorkflow, QMainWindow):
    def __init__(self, engine=None, store=None, audit=None, instance_lock=None,
                 update_client=None, check_on_start=False):
        super().__init__()
        self.engine = engine or Engine(resource_dir() / "vendor" / "windivert")
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
        self.help_dialog = None
        self.changelog_dialog = None
        self.jobs = Jobs()
        self.jobs.done.connect(self.job_done)
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(1320, 940)
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
        self.refresh_privilege()
        self.name_field.setFocus()
        self.record("app_open", version=__version__, admin=is_admin())
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.tick)
        self.timer.timeout.connect(self.refresh_theme_state)
        self.timer.start()
        self.initialize_updates(update_client, check_on_start)

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

    def toggle_advanced_section(self, checked):
        self.advanced_panel.setVisible(checked)
        self.advanced_toggle.setText(("▾" if checked else "▸") + "  高级选项 · 短时断网、重复与乱序")

    def sync_advanced_section(self):
        enabled = (self.blackout.isChecked() or self.numbers["duplicate_pct"].value() > 0
                   or self.numbers["reorder_pct"].value() > 0)
        self.advanced_toggle.setChecked(enabled)
        self.toggle_advanced_section(enabled)

    def build_ui(self):
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(244)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 24, 16, 16)
        side.setSpacing(10)
        brand_row = QHBoxLayout()
        logo = label("")
        pixmap = QPixmap(str(resource_dir() / LOGO_PATH))
        if not pixmap.isNull():
            logo.setPixmap(pixmap.scaled(42, 42, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
            brand_row.addWidget(logo)
        brand_row.addWidget(label(APP_NAME, "brand"), 1)
        side.addLayout(brand_row)
        side.addWidget(label(APP_SUBTITLE, "muted"))
        side.addWidget(label("游戏测试专用 · Windows x64", "muted"))
        side.addSpacing(16)
        side.addWidget(label("场景库", "section"))
        side.addWidget(label("选择场景，配置后开始测试", "muted"))
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
        side.addWidget(self.presets, 1)
        scene_actions = QHBoxLayout()
        self.new_button = QPushButton("新建场景")
        self.new_button.clicked.connect(lambda: self.new_scenario())
        scene_actions.addWidget(self.new_button)
        self.delete_button = QPushButton("删除场景")
        self.delete_button.clicked.connect(lambda: self.delete_scenario())
        scene_actions.addWidget(self.delete_button)
        side.addLayout(scene_actions)
        transfer = QHBoxLayout()
        self.import_button = QPushButton("导入场景")
        self.import_button.clicked.connect(self.import_config)
        transfer.addWidget(self.import_button)
        self.export_button = QPushButton("导出场景")
        self.export_button.clicked.connect(self.export_config)
        transfer.addWidget(self.export_button)
        side.addLayout(transfer)
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
        right.setContentsMargins(24, 24, 24, 18)
        right.setSpacing(12)
        header = QHBoxLayout()
        text = QVBoxLayout()
        title = label("让网络问题，稳定复现。", "title")
        title.setWordWrap(True)
        text.addWidget(title)
        subtitle = label("选择测试场景，设置目标流量与网络条件。", "muted")
        subtitle.setWordWrap(True)
        text.addWidget(subtitle)
        header.addLayout(text, 1)
        self.status = label("●  待启动", "badge")
        header.addWidget(self.status, 0, Qt.AlignmentFlag.AlignTop)
        right.addLayout(header)
        self.run_banner = label("当前未运行弱网场景", "run_banner")
        self.run_banner.setWordWrap(True)
        right.addWidget(self.run_banner)

        controls, bar = panel(None)
        controls.setObjectName("control_card")
        actions = QHBoxLayout()
        actions.setSpacing(12)
        scene = QVBoxLayout()
        scene.addWidget(label("当前编辑场景", "muted"))
        self.name_field = QLineEdit()
        self.name_field.setObjectName("scene_name")
        self.name_field.setPlaceholderText("输入场景名称")
        self.name_field.setToolTip("修改名称后保存；使用“另存为”将当前参数保留为新的场景。")
        self.name_field.setMinimumWidth(80)
        scene.addWidget(self.name_field)
        actions.addLayout(scene, 1)
        self.start_button = QPushButton("▶  开始测试")
        self.start_button.setObjectName("start")
        self.start_button.clicked.connect(self.start_test)
        self.stop_button = QPushButton("■  停止并恢复")
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_test)
        actions.addWidget(self.start_button, 0, Qt.AlignmentFlag.AlignBottom)
        actions.addWidget(self.stop_button, 0, Qt.AlignmentFlag.AlignBottom)
        bar.addLayout(actions)
        self.scene_hint = label("可编辑参数，另存为自己的测试场景。", "scene_hint")
        self.scene_hint.setWordWrap(True)
        bar.addWidget(self.scene_hint)
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
        bar.addLayout(info)
        right.addWidget(controls)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        content.setObjectName("scroll_content")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)
        body.setSpacing(14)
        self.settings = QWidget()
        settings_layout = QVBoxLayout(self.settings)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(14)
        target, target_layout = panel("1   流量目标")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.scope = combo([("整台电脑（非回环）", "all"), ("指定 IP / 端口", "endpoint"),
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
        self.host.setPlaceholderText("服务器 IP，例如 192.168.1.20")
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
        self.blackout = QCheckBox("完全断网（丢弃所有匹配流量）")
        self.blackout.setToolTip("仅对选中的流量生效；可等待 N 秒后自动断网，再在 M 秒后恢复。")
        advanced_layout.addWidget(self.blackout)
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
        stats_row = QHBoxLayout()
        self.stats = {}
        for key, title in [("received", "已捕获 / 包"), ("sent", "已转发 / 包"),
                           ("dropped", "已丢弃 / 包"), ("queued", "等待发送 / 包")]:
            card, layout = panel(None)
            layout.addWidget(label(title, "muted"))
            self.stats[key] = label("0", "number")
            layout.addWidget(self.stats[key])
            stats_row.addWidget(card)
        body.addLayout(stats_row)
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
        body.addWidget(logs)
        body.addStretch()
        self.scroll.setWidget(content)
        right.addWidget(self.scroll, 1)
        outer.addLayout(right, 1)
        self.scope.currentIndexChanged.connect(self.update_scope)
        for c in (self.protocol, self.direction):
            c.currentIndexChanged.connect(self.update_preview)
        for field in (self.host, self.ports, self.advanced):
            field.textChanged.connect(self.update_preview)
        self.blackout.toggled.connect(self.update_blackout)
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
                    duration_s=self.duration.value(), reorder_ms=self.reorder_ms.value())
        data.update({key: field.value() for key, field in self.numbers.items()})
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

    def update_scope(self, *_):
        if self.loading:
            return
        scope = self.scope.currentData()
        self.host.setVisible(scope == "endpoint")
        self.ports.setVisible(scope == "endpoint")
        self.advanced.setVisible(scope == "advanced")
        self.protocol.setEnabled(scope != "advanced")
        self.direction.setEnabled(scope not in ("advanced", "loopback"))
        if scope == "loopback":
            self.direction.setCurrentIndex(self.direction.findData("outbound"))
        notes = {
            "all": "作用于整台电脑的匹配流量。可切换“指定 IP / 端口”，集中测试游戏服务器连接。",
            "endpoint": "填写服务器 IP 和端口后只影响该目标。网页不能直接填 URL：先将域名解析成 IP；HTTPS 常用端口 443，HTTP 常用端口 80。多个端口用英文逗号分隔。",
            "loopback": "用于本机客户端与本机服务测试。WinDivert 的回环包统一按上行处理。",
            "advanced": "以表达式定义全部匹配条件；上方协议和方向在此模式下不参与过滤。",
        }
        self.scope_note.setText(notes[scope])
        self.update_preview()

    def update_blackout(self, *_):
        if self.loading:
            return
        for field in self.numbers.values():
            field.setEnabled(not self.blackout.isChecked())
        self.reorder_ms.setEnabled(not self.blackout.isChecked())
        self.duration.setEnabled(not self.blackout.isChecked())
        self.blackout_delay.setEnabled(self.blackout.isChecked())
        self.blackout_duration.setEnabled(self.blackout.isChecked())
        self.blackout_loop.setEnabled(self.blackout.isChecked())
        if not self.blackout.isChecked():
            self.blackout_loop.setChecked(False)
        if self.blackout_loop.isChecked():
            self.blackout_hint.setText(
                f"循环：正常联网 {self.blackout_delay.value()} 秒 → 断网 {self.blackout_duration.value()} 秒 → 恢复后重复，直到停止。"
                "两段时间均需至少 1 秒。")
        else:
            self.blackout_hint.setText("单次执行，结束后自动恢复；延后断网大于 0 秒时，持续时间至少为 1 秒。")

    def update_preview(self, *_):
        if self.loading:
            return
        try:
            self.filter_preview.setText(build_filter(self.collect()))
        except (ValueError, TypeError) as exc:
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
        if self.busy or self.update_busy:
            return
        if self.is_current_scene_running():
            self.set_running_controls()
            return
        if not is_admin():
            self.error(
                "请先以管理员身份重启后使用。\n\n"
                "点击“以管理员身份重启”，在 Windows 安全提示中选择“是”，"
                "然后在新打开的窗口中点击“开始测试”。",
                title="需要管理员权限",
            )
            return
        try:
            config = validate_config(self.collect())
            self.engine.validate_filter(config["filter"])
            self.store.save(config)
            # Starting is refused if its persistent audit record cannot be written.
            row = self.audit.write("test_start_requested", config=config)
            self.session_log = [row]
        except (OSError, ValueError, RuntimeError) as exc:
            self.error(str(exc))
            return
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

    def stop_test(self):
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
            if name == "switch_start":
                self.session_active = False
            elif name in ("switch_stop", "update_stop"):
                self.session_active = bool(self.engine.snapshot().get("running"))
            else:
                self.session_active = False
            self.record("test_error", operation=name, error=error)
            self.status.setText("●  启动失败" if name in ("start", "switch_start") else "●  恢复失败")
            self.run_banner.setText("场景切换失败，网络已恢复；请检查参数后重试。" if name == "switch_start" else "操作失败，请检查日志。")
            self.error(error)
        elif name in ("start", "switch_start"):
            self.session_active = True
            self.record("test_started", config=self.session_config)
            self.append_log(f"开始「{self.session_config['name']}」；过滤条件：{self.session_config['filter']}")
            self.run_banner.setText(f"正在运行：「{self.session_config['name']}」")
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
        editing = not self.busy and not self.session_active and not self.update_busy
        self.settings.setEnabled(editing)
        # Presets remain selectable while a session runs; selection only edits
        # the candidate form and never changes the active engine configuration.
        self.presets.setEnabled(not self.busy and not self.update_busy)
        self.import_button.setEnabled(editing)
        self.export_button.setEnabled(editing)
        self.admin_button.setEnabled(editing)
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
        self.save_as_button.setEnabled(not self.busy and not self.update_busy)
        same_scene = self.is_current_scene_running()
        self.start_button.setEnabled((editing or (self.session_active and not self.busy)) and not same_scene and not self.update_busy)
        self.start_button.setText("当前场景运行中" if same_scene else ("↻  切换并启动" if self.session_active else "▶  开始测试"))
        if self.session_active:
            self.start_button.setToolTip("当前场景已生效" if same_scene else "先恢复当前网络，再启动左侧选中的新场景")
        else:
            self.start_button.setToolTip("开始测试；首次使用请先以管理员身份重启")
        self.stop_button.setEnabled(self.session_active and not self.busy)
        self.refresh_theme_state()

    def finish_session(self, reason):
        self.session_active = False
        snapshot = self.engine.snapshot()
        self.record("test_stopped", reason=reason, stats=snapshot)
        self.append_log(f"测试结束 · {reason}；已释放拦截句柄。捕获 {snapshot.get('received', 0)} 包，丢弃 {snapshot.get('dropped', 0)} 包。")
        self.status.setText("●  已停止")
        self.run_banner.setText("当前未运行弱网场景")
        self.run_note.setText("已停止拦截。应用连接是否恢复可继续在游戏中验收。")
        self.set_running_controls()

    def tick(self):
        snapshot = self.engine.snapshot()
        for key, field in self.stats.items():
            field.setText(f"{snapshot.get(key, 0):,}")
        self.detail_stats.setText(f"重复 {snapshot.get('duplicated', 0)} · 乱序触发 {snapshot.get('reordered', 0)} · 错误 {snapshot.get('errors', 0)}")
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
                looping = self.session_config.get("blackout_loop", False)
                cycle = f"循环 · 第 {snapshot.get('cycle', 1)} 轮 · " if looping else ""
                if phase == "waiting":
                    wait_remaining = max(0.0, float(snapshot.get("wait_remaining", 0)))
                    self.status.setText(f"●  {cycle}等待断网")
                    self.run_note.setText(
                        f"{name}  ·  {cycle}等待 {math.ceil(wait_remaining)} 秒后自动断网"
                    )
                    self.run_banner.setText(f"正在运行：「{name}」\n{cycle}网络正常 · 等待 {math.ceil(wait_remaining)} 秒后自动断网")
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
                    self.run_banner.setText(f"正在运行：「{name}」\n{cycle}断网进行中 · {suffix}")
                    return

                duration = self.session_config.get("duration_s", 0)
                remaining = max(0, duration - int(elapsed)) if duration else None
                suffix = f"{remaining} 秒后自动恢复" if remaining is not None else "手动停止"
                self.status.setText("●  测试进行中")
                self.run_note.setText(f"{name}  ·  已运行 {int(elapsed)} 秒  ·  {suffix}")
                self.run_banner.setText(f"正在运行：「{name}」\n正常弱网条件处理中 · {suffix}")

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
        <ol><li>选择左侧场景，或点击“导入场景配置”加载 JSON 文件。</li><li>按需填写服务器 IP / 端口，确认过滤条件预览。</li><li>以管理员身份运行后点击“开始测试”。运行中设置会锁定，延迟断网倒计时也从此刻开始。</li><li>测试完成点击“停止并恢复”；也可设置自动恢复秒数。等待断网或断网进行中都可以随时停止。</li></ol>
        <p>顶部显示实际运行场景。同一组参数已生效时，按钮显示“当前场景运行中”；选择另一场景后可点击“切换并启动”，先恢复网络再启动新场景。</p>
        <h3>二、流量目标</h3>
        <p><b>整台电脑</b>：处理除本机回环外的匹配流量。<br><b>指定 IP / 端口</b>：仅匹配目标地址或端口。网页不能直接填写 URL，应先将域名解析成 IP；HTTP 常用 80，HTTPS 常用 443。多个端口用英文逗号分隔。<br><b>本机回环</b>：测试 127.0.0.1 等本机服务。<br><b>高级过滤表达式</b>：直接编写 WinDivert 条件；会覆盖协议和方向选择。</p>
        <h3>三、网络条件参数</h3>
        <p><b>延迟</b>：每个数据包增加的单程毫秒数。双向 150 ms 约增加 300 ms 往返时延。<br><b>延迟抖动 ±</b>：在延迟值附近随机增减，可能造成包顺序变化。<br><b>随机丢包</b>：按百分比随机丢弃数据包。<br><b>每方向带宽上限</b>：上下行分别限速，单位 KiB/s；0 表示不限速。<br><b>重复包概率</b>：命中后额外发送一份相同数据包。<br><b>乱序触发概率 / 暂存</b>：暂存命中的包，让后续包先发送。<br><b>完全断网</b>：勾选后，点击“开始测试”即开始计时；“延后断网 N 秒”会先保持网络正常，倒计时结束后自动丢弃匹配流量，并持续“断网持续 M 秒”，随后自动恢复。等待或断网期间都可点击“停止并恢复”。<br><b>断网持续为0</b>：显示“手动停止”，仅在延后断网为0秒时有效。<br><b>普通测试自动恢复</b>：非断网模式下的倒计时；0 表示手动停止。</p>
        <p><b>循环执行</b>：在完全断网选项下勾选，重复“正常联网 N 秒 → 断网 M 秒 → 恢复网络”，直到点击“停止并恢复”。两段时间均需至少 1 秒。顶部显示当前轮次和阶段倒计时，循环开关可随场景保存；默认关闭，执行一次。</p>
        <h3>四、我的场景与皮肤</h3>
        <p><b>新建场景</b>：输入名称，从正常网络开始创建，调整参数后点击“保存修改”。<br><b>另存为新场景</b>：复制当前全部目标和参数，原场景仍保留。<br><b>保存修改</b>：更新选中的自定义场景。内置场景始终保留，修改后请另存为。名称不可为空、与其他场景重复或超过80字。保存后可从左侧直接选择，重启也会保留。<br><b>删除场景</b>：选中已保存的自定义场景，点击左侧“删除场景”并确认。删除后回到“正常对照”，不会启动测试；内置场景不可删除，测试或更新期间暂不可删除。<br><b>导入与导出</b>：用 JSON 文件与同事分享参数；导入后点击保存即可加入场景库。<br><b>皮肤</b>：左下角可切换蓝白、深色、青绿，自动记住选择，不影响正在运行的测试。</p>
        <h3>五、恢复与记录</h3>
        <p><b>检查更新</b>：启动后自动在后台检查 GitHub 最新版本，也可点击左下角“检查更新”。发现新版本后可选择“下载并更新”或“稍后”。确认更新会停止弱网测试、恢复网络，下载并校验安装包，然后打开安装向导。检查失败不影响正常使用；网络需能访问 GitHub。</p>
        <p>仅处理过滤条件匹配的数据包，不记录包内容。停止、自动恢复、关闭窗口和快捷键 Ctrl + Alt + F12 均会释放拦截句柄。可导出场景配置和测试记录供复盘。</p>
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
        if self.update_busy:
            self.close_pending = True
            self.update_cancel.set()
            event.ignore()
            return
        if self.busy or self.session_active:
            self.close_pending = True
            self.stop_test()
            event.ignore()
            return
        if self.engine.snapshot().get("running"):
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
