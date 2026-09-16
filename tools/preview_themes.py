"""Render all built-in Motu Net themes for visual QA without opening WinDivert."""
from __future__ import annotations
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from netlab.ui import MainWindow
from netlab.storage import ConfigStore, AuditLog

class PreviewEngine:
    def __init__(self):
        self.state = {"running": False, "received": 0, "sent": 0, "dropped": 0, "queued": 0, "errors": 0, "error": None, "elapsed": 0}
    def validate_filter(self, value):
        return None
    def start(self, config): self.state["running"] = True
    def stop(self): self.state["running"] = False
    def snapshot(self): return dict(self.state)

app = QApplication([])
for font in (Path(r"C:/Windows/Fonts/msyh.ttc"), Path(r"C:/Windows/Fonts/msyhbd.ttc"),
             Path(r"C:/Windows/Fonts/seguisym.ttf"), Path(r"C:/Windows/Fonts/segoeui.ttf")):
    if font.exists():
        QFontDatabase.addApplicationFont(str(font))
out = ROOT / "artifacts" / "theme-previews"
out.mkdir(parents=True, exist_ok=True)
for theme in ("blue", "dark", "teal"):
    # Separate stores keep each run's selected theme deterministic.
    temporary = tempfile.TemporaryDirectory(prefix="motu-theme-preview-")
    store_root = Path(temporary.name)
    window = MainWindow(engine=PreviewEngine(), store=ConfigStore(store_root), audit=AuditLog(store_root))
    window.apply_theme(theme, persist=False)
    window.show()
    window.resize(1320, 940)
    window.presets.setCurrentRow(4)
    app.processEvents()
    path = out / (theme + ".png")
    window.grab().save(str(path))
    print(path)
    if theme == "blue":
        window.resize(980, 680)
        app.processEvents()
        path = out / "blue-narrow.png"
        window.grab().save(str(path))
        print(path)
        window.show_help()
        app.processEvents()
        window.help_dialog.grab().save(str(out / "blue-help.png"))
        window.apply_theme("dark", persist=False)
        app.processEvents()
        window.help_dialog.grab().save(str(out / "dark-help.png"))
        window.apply_theme("blue", persist=False)
        window.help_dialog.close()
        window.resize(1320, 940)
        window.scroll.verticalScrollBar().setValue(0)
        window.session_config = window.collect()
        window.engine.state.update(running=True, elapsed=12, received=2048, sent=1980)
        window.session_active = True
        window.set_running_controls()
        window.tick()
        app.processEvents()
        window.grab().save(str(out / "blue-running.png"))
        window.engine.stop()
        window.session_active = False
        window.set_running_controls()
        window.finish_session("预览结束")
        window.presets.setCurrentRow(6)
        window.advanced_toggle.setChecked(True)
        app.processEvents()
        window.scroll.verticalScrollBar().setValue(280)
        app.processEvents()
        path = out / "blue-advanced.png"
        window.grab().save(str(path))
        print(path)
    window.close()
    app.processEvents()
    temporary.cleanup()
