"""Render NetLab's own widget tree for visual QA; never opens the driver."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from netlab.ui import MainWindow
from netlab.storage import ConfigStore, AuditLog

app = QApplication([])
with tempfile.TemporaryDirectory() as temporary:
    window = MainWindow(store=ConfigStore(temporary), audit=AuditLog(temporary))
    window.resize(1320, 1020)
    window.show()
    app.processEvents()
    output = Path(__file__).resolve().parents[1] / "artifacts"
    output.mkdir(exist_ok=True)
    window.grab().save(str(output / "NetLab-preview.png"))
    window.close()
    app.processEvents()
print(output / "NetLab-preview.png")
