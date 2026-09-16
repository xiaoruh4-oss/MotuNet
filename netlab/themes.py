"""Shared widget styling for the three built-in Motu Net skins."""
from __future__ import annotations
from .platform_win import resource_dir
THEMES = {
    "blue": {"name": "蓝白", "bg": "#f5f8fc", "side": "#edf4fc", "surface": "#ffffff", "soft": "#eef5fd", "text": "#132c45", "muted": "#60758a", "border": "#d4e1ef", "input": "#ffffff", "accent": "#086ad8", "hover": "#1680eb", "selected": "#dcecff", "disabled": "#eef1f5", "disabled_text": "#8b9bab", "success": "#128257", "success_bg": "#e8f8f0", "warning": "#985c06", "warning_bg": "#fff3db", "danger": "#cb434e", "danger_hover": "#ad303a", "scroll": "#bacbdc"},
    "dark": {"name": "深色", "bg": "#101922", "side": "#15222e", "surface": "#1a2b3a", "soft": "#1d3347", "text": "#edf4fc", "muted": "#b0c3d6", "border": "#365068", "input": "#112231", "accent": "#3888df", "hover": "#4c9bef", "selected": "#254668", "disabled": "#22313e", "disabled_text": "#8294a5", "success": "#6eddb1", "success_bg": "#153f35", "warning": "#ffd080", "warning_bg": "#493b22", "danger": "#cb4c5b", "danger_hover": "#df6170", "scroll": "#46647b"},
    "teal": {"name": "青绿", "bg": "#eff8f6", "side": "#e3f1ed", "surface": "#ffffff", "soft": "#eaf6f2", "text": "#153e38", "muted": "#577c75", "border": "#c8e1d9", "input": "#ffffff", "accent": "#07846d", "hover": "#159d82", "selected": "#d0eee4", "disabled": "#edf3f0", "disabled_text": "#8a9d96", "success": "#087c54", "success_bg": "#def7e9", "warning": "#936019", "warning_bg": "#fff2db", "danger": "#c94450", "danger_hover": "#b63540", "scroll": "#a9cfc2"},
}
def stylesheet(theme="blue"):
    p = dict(THEMES.get(theme, THEMES["blue"]))
    icon_tone = "dark" if theme == "dark" else "light"
    p["up_icon"] = (resource_dir() / "assets" / "ui" / f"chevron-up-{icon_tone}.svg").as_posix()
    p["down_icon"] = (resource_dir() / "assets" / "ui" / f"chevron-down-{icon_tone}.svg").as_posix()
    return """
QWidget {{ color: {text}; font-family: 'Microsoft YaHei UI', 'Segoe UI Symbol'; font-size: 13px; }}
QMainWindow, QDialog, QWidget#root {{ background: {bg}; }}
QFrame#sidebar {{ background: {side}; border-right: 1px solid {border}; }}
QFrame#card {{ background: {surface}; border: 1px solid {border}; border-radius: 10px; }}
QFrame#control_card {{ background: {soft}; border: 1px solid {border}; border-radius: 10px; }}
QFrame#condition_card {{ background: {soft}; border: 1px solid {border}; border-radius: 8px; }}
QLabel#title {{ font-size: 27px; font-weight: 700; color: {text}; }}
QLabel#brand {{ font-size: 23px; font-weight: 700; }}
QLabel#section {{ font-size: 18px; font-weight: 700; }}
QLabel#condition_title {{ font-size: 14px; font-weight: 700; color: {text}; }}
QLabel#muted, QLabel#scene_hint {{ color: {muted}; font-size: 12px; }}
QLabel#number {{ font-size: 24px; font-weight: 700; color: {accent}; }}
QLabel#badge {{ color: {success}; background: {success_bg}; border-radius: 6px; padding: 8px 12px; font-weight: 600; }}
QLabel#badge[active="true"] {{ color: {warning}; background: {warning_bg}; }}
QLabel#admin_status {{ color: {warning}; font-size: 12px; }}
QLabel#admin_status[ready="true"] {{ color: {success}; }}
QLabel#run_banner {{ color: {muted}; background: {surface}; border: 1px solid {border}; border-radius: 7px; padding: 8px 12px; }}
QLabel#run_banner[active="true"] {{ color: {warning}; background: {warning_bg}; border: 1px solid {warning}; font-weight: 700; font-size: 14px; }}
QPushButton {{ color: {text}; background: {surface}; border: 1px solid {border}; border-radius: 6px; padding: 8px 12px; }}
QPushButton:hover {{ background: {selected}; border-color: {accent}; }}
QPushButton:disabled {{ color: {disabled_text}; background: {disabled}; border-color: {border}; }}
QPushButton#start, QPushButton#primary {{ background: {accent}; color: white; border: 1px solid {accent}; font-weight: 700; }}
QPushButton#start {{ font-size: 16px; padding: 11px 18px; }}
QPushButton#start:hover, QPushButton#primary:hover {{ background: {hover}; }}
QPushButton#stop {{ background: {danger}; color: white; border: 1px solid {danger}; font-weight: 700; padding: 12px 15px; }}
QPushButton#stop:hover {{ background: {danger_hover}; }}
QPushButton#start:disabled, QPushButton#primary:disabled, QPushButton#stop:disabled {{ background: {disabled}; color: {disabled_text}; border-color: {border}; }}
QPushButton#link {{ color: {accent}; background: transparent; border: none; padding: 6px 4px; text-align: left; }}
QPushButton#advanced_toggle {{ color: {accent}; background: {soft}; border: none; font-weight: 600; text-align: left; padding: 10px 12px; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {{ color: {text}; background: {input}; border: 1px solid {border}; border-radius: 5px; padding: 7px; selection-background-color: {accent}; selection-color: white; }}
QLineEdit#scene_name {{ font-size: 16px; font-weight: 600; padding: 9px; }}
QLineEdit#filter_preview {{ color: {muted}; background: {soft}; font-size: 12px; padding: 6px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {accent}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {disabled_text}; background: {disabled}; }}
QComboBox QAbstractItemView {{ color: {text}; background: {surface}; selection-background-color: {selected}; selection-color: {text}; }}
QComboBox {{ padding-right: 25px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right; width: 24px; border: none; background: transparent; }}
QComboBox::down-arrow {{ image: url("{down_icon}"); width: 12px; height: 12px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 23px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; height: 17px; border-left: 1px solid {border}; border-bottom: 1px solid {border}; background: {soft}; border-top-right-radius: 4px; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; height: 17px; border-left: 1px solid {border}; background: {soft}; border-bottom-right-radius: 4px; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{up_icon}"); width: 12px; height: 12px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{down_icon}"); width: 12px; height: 12px; }}
QMenu {{ color: {text}; background: {surface}; border: 1px solid {border}; padding: 5px; }}
QMenu::item {{ padding: 7px 30px 7px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {selected}; color: {text}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
QListWidget {{ background: transparent; border: none; outline: 0; }}
QListWidget::item {{ padding: 11px 9px; margin: 3px 0; border: 1px solid transparent; border-radius: 6px; }}
QListWidget::item:selected {{ color: {accent}; background: {selected}; border-color: {border}; }}
QListWidget::item:hover {{ background: {soft}; }}
QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {muted}; border-radius: 4px; background: {input}; }}
QCheckBox::indicator:checked {{ background: {accent}; border: 3px solid {selected}; }}
QScrollArea {{ border: none; background: {bg}; }}
QWidget#scroll_content, QWidget#settings {{ background: {bg}; }}
QScrollBar:vertical {{ width: 8px; background: transparent; }}
QScrollBar::handle:vertical {{ background: {scroll}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QToolTip {{ background: {surface}; color: {text}; border: 1px solid {border}; padding: 7px; }}
QMessageBox {{ background: {surface}; }}
QMessageBox QLabel {{ color: {text}; min-width: 360px; }}
QTextBrowser {{ padding: 16px; font-size: 14px; }}
""".format(**p)
