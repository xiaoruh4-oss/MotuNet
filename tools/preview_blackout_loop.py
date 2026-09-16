"""Render blackout loop controls and running states without touching traffic."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from netlab.config import DEFAULT_CONFIG
from netlab.storage import AuditLog, ConfigStore
from netlab.ui import MainWindow


class PreviewEngine:
    def __init__(self):
        self.state = {"running": False}

    def snapshot(self):
        return dict(self.state)

    def stop(self):
        self.state["running"] = False


app = QApplication([])
for name in ("msyh.ttc", "msyhbd.ttc", "seguisym.ttf", "segoeui.ttf"):
    path = Path("C:/Windows/Fonts") / name
    if path.exists():
        QFontDatabase.addApplicationFont(str(path))
out = ROOT / "artifacts" / "loop-previews"
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="motu-loop-preview-") as temporary:
    engine = PreviewEngine()
    window = MainWindow(engine=engine, store=ConfigStore(temporary), audit=AuditLog(temporary))
    window.load_form(dict(DEFAULT_CONFIG, name="延迟断网", blackout=True,
                          start_delay_s=10, blackout_duration_s=5, blackout_loop=True))
    window.show()
    for theme in ("blue", "dark", "teal"):
        window.apply_theme(theme, persist=False)
        for width, height in ((1320, 1040), (980, 680)):
            window.resize(width, height)
            app.processEvents()
            window.scroll.ensureWidgetVisible(window.blackout_hint, 0, 25)
            app.processEvents()
            window.grab().save(str(out / f"{theme}-{width}.png"))
    window.apply_theme("blue", persist=False)
    window.resize(980, 680)
    window.session_config = window.collect()
    window.session_active = True
    window.set_running_controls()
    for phase in ("waiting", "active"):
        engine.state.update(running=True, phase=phase, cycle=2, wait_remaining=7.1,
                            active_remaining=3.1, active_elapsed=1.9, elapsed=17)
        window.tick()
        app.processEvents()
        window.grab().save(str(out / f"running-{phase}-980.png"))
    engine.stop()
    window.session_active = False
    window.close()
    app.processEvents()
print(out)
