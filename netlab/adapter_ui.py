"""Physical adapter selection; opening this dialog never changes networking."""
import threading

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .themes import stylesheet


class AdapterResults(QObject):
    loaded = Signal(object, str)


class AdapterPicker(QDialog):
    def __init__(self, parent, selected, *, loader=None):
        super().__init__(parent)
        self.setWindowTitle("选择要断开的网卡")
        self.resize(620, 400)
        self.setStyleSheet(stylesheet(parent.theme_id))
        self.selected = set(selected)
        self.adapters = []
        self.checks = []
        layout = QVBoxLayout(self)
        self.note = QLabel("正在读取物理网卡…")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.list_widget = QWidget()
        self.rows = QVBoxLayout(self.list_widget)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(self.list_widget)
        layout.addWidget(area, 1)
        scope = QLabel("会禁用所选网卡，影响其上的所有应用；未选中的网卡仍可联网。")
        scope.setWordWrap(True)
        layout.addWidget(scope)
        buttons = QHBoxLayout()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.confirm = QPushButton("使用所选网卡")
        self.confirm.setEnabled(False)
        self.confirm.clicked.connect(self.accept)
        buttons.addWidget(self.confirm)
        layout.addLayout(buttons)
        self.results = AdapterResults(self)
        self.results.loaded.connect(self.loaded)

        def load():
            try:
                if loader:
                    adapters = loader()
                else:
                    from .adapter_control import list_adapters
                    adapters = list_adapters()
                self.results.loaded.emit(adapters, "")
            except Exception as exc:
                self.results.loaded.emit([], str(exc))

        threading.Thread(target=load, daemon=True, name="Motu-adapter-list").start()

    def loaded(self, adapters, error):
        self.adapters = adapters
        self.note.setText("读取网卡失败：" + error if error else
                          "选择当前使用的以太网 / Wi-Fi；同时联网时可多选。")
        if not error and not adapters:
            self.note.setText("没有找到可用的物理网卡。")
        for adapter in adapters:
            suffix = "已连接" if adapter.get("connected") else "已启用，未连接"
            if not adapter.get("admin_up"):
                suffix = "已禁用，不能用于本次断网"
            check = QCheckBox(f"{adapter['name']} · {suffix}")
            check.setToolTip(f"{adapter['name']}\n{adapter.get('description', '')}")
            check.setEnabled(bool(adapter.get("admin_up")))
            check.setChecked(adapter["guid"] in self.selected and check.isEnabled())
            check.toggled.connect(self.update_selection)
            self.rows.addWidget(check)
            description = QLabel(adapter.get("description", ""))
            description.setWordWrap(True)
            description.setContentsMargins(26, 0, 0, 6)
            description.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            description.setEnabled(check.isEnabled())
            self.rows.addWidget(description)
            self.checks.append((check, adapter))
        self.rows.addStretch(1)
        self.update_selection()

    def update_selection(self, *_):
        self.confirm.setEnabled(any(check.isChecked() for check, _ in self.checks))

    def chosen(self):
        return [adapter for check, adapter in self.checks if check.isChecked()]
