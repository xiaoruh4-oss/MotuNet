"""Render update entry and notice with an offline client and isolated settings."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication
from netlab import __version__
from netlab.ui import MainWindow
from netlab.storage import ConfigStore, AuditLog
from netlab.updater import UpdateInfo


class PreviewEngine:
    def snapshot(self):
        return {"running": False}

    def stop(self):
        pass


app = QApplication([])
for name in ("msyh.ttc", "msyhbd.ttc", "seguisym.ttf", "segoeui.ttf"):
    QFontDatabase.addApplicationFont(str(Path("C:/Windows/Fonts") / name))
out = ROOT / "artifacts" / "update-previews"
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="motu-update-preview-") as temporary:
    window = MainWindow(engine=PreviewEngine(), store=ConfigStore(temporary), audit=AuditLog(temporary))
    window.resize(980, 680)
    window.show()
    info = UpdateInfo("0.5.1", "https://github.com/example/MotuNet/releases/download/v0.5.1/MotuNet-0.5.1-Setup-x64.exe",
                      "a" * 64, 33966121, "优化延迟断网与更新体验。\n修复已知问题。")
    for theme in ("blue", "dark", "teal"):
        window.apply_theme(theme, persist=False)
        app.processEvents()
        window.grab().save(str(out / f"{theme}-main.png"))
        window.update_checked(info, "", True)
        app.processEvents()
        window.update_dialog.grab().save(str(out / f"{theme}-available.png"))
        window.update_dialog.close()
        window.update_message("检查更新", f"当前已是最新版本：{__version__}")
        app.processEvents()
        window.update_dialog.grab().save(str(out / f"{theme}-current.png"))
        window.update_dialog.close()
    window.close()
    app.processEvents()
print(out)
