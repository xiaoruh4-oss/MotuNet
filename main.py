"""Desktop entry point. No interception starts until the user clicks Start."""
import os
import sys


def main():
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtGui import QFont
    from netlab.platform_win import InstanceLock
    from netlab.storage import data_dir
    from netlab.ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Motu Net")
    app.setOrganizationName("Motu")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    lock = InstanceLock(data_dir()) if os.name == "nt" else None
    if lock and not lock.acquire():
        QMessageBox.information(None, "Motu Net 已运行", "请使用已经打开的弱网实验室窗口。")
        return 0
    window = MainWindow(instance_lock=lock, check_on_start=True)
    window.show()
    try:
        return app.exec()
    finally:
        window.engine.stop()
        if lock:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
