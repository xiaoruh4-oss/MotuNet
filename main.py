"""Desktop entry point. No interception starts until the user clicks Start."""
import os
import sys


def main():
    if "--adapter-worker" in sys.argv[1:]:
        from netlab.adapter_engine import worker_main
        index = sys.argv.index("--adapter-worker")
        if len(sys.argv) != index + 2:
            raise SystemExit("Missing adapter session path")
        return worker_main(sys.argv[index + 1])
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtGui import QFont
    from netlab.platform_win import InstanceLock, resource_dir
    from netlab.storage import AuditLog, ConfigStore, data_dir
    from netlab.ui import MainWindow

    local_test = "--local-test" in sys.argv[1:] and not getattr(sys, "frozen", False)
    test_root = resource_dir() / ".localdata" / "local-test" if local_test else None
    app = QApplication(sys.argv)
    app.setApplicationName("Motu Net")
    app.setOrganizationName("Motu")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    lock = InstanceLock(test_root or data_dir()) if os.name == "nt" else None
    if lock and not lock.acquire():
        QMessageBox.information(None, "Motu Net 已运行", "请使用已经打开的弱网实验室窗口。")
        return 0
    window = MainWindow(instance_lock=lock, store=ConfigStore(test_root) if local_test else None,
                        audit=AuditLog(test_root) if local_test else None,
                        check_on_start=not local_test)
    if local_test:
        window.setWindowTitle(f"{window.windowTitle()} · 本地测试版")
        window.update_button.hide()
    window.show()
    window.showNormal()
    window.raise_()
    window.activateWindow()
    try:
        return app.exec()
    finally:
        if not getattr(window, "adapter_recovery_handed_off", False):
            window.engine.stop()
        if lock:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
