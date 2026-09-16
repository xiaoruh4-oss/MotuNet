"""Background update checks and user-confirmed installer handoff."""
from pathlib import Path
import threading

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from . import __version__
from .platform_win import launch_installer
from .themes import stylesheet
from .update_source import UPDATE_REPOSITORY
from .updater import UpdateClient, UpdateCancelled


class UpdateSignals(QObject):
    checked = Signal(object, str, bool)
    downloaded = Signal(object, str, bool)
    progress = Signal(int, int)


class UpdateWorkflow:
    def initialize_updates(self, client=None, check_on_start=False):
        self.update_client = client
        self.update_checking = False
        self.update_busy = False
        self.updates_closed = False
        self.update_info = None
        self.pending_update_notice = None
        self.update_dialog = None
        self.update_progress_dialog = None
        self.update_cancel = threading.Event()
        self.update_signals = UpdateSignals(self)
        self.update_signals.checked.connect(self.update_checked)
        self.update_signals.downloaded.connect(self.update_downloaded)
        self.update_signals.progress.connect(self.update_download_progress)
        self.update_start_timer = QTimer(self)
        self.update_start_timer.setSingleShot(True)
        self.update_start_timer.timeout.connect(lambda: self.check_updates(manual=False))
        self.update_notice_timer = QTimer(self)
        self.update_notice_timer.setInterval(750)
        self.update_notice_timer.timeout.connect(self.deliver_update_notice)
        if check_on_start:
            self.update_start_timer.start(1500)

    def check_updates(self, manual=True):
        if self.updates_closed or self.update_checking or self.update_busy:
            return
        self.update_start_timer.stop()
        self.update_checking = True
        self.update_button.setEnabled(False)
        self.update_button.setText("正在检查更新…")
        signals = self.update_signals

        def check():
            info, error = None, ""
            try:
                if self.update_client is None:
                    self.update_client = UpdateClient(UPDATE_REPOSITORY, __version__)
                info = self.update_client.check()
            except Exception as exc:
                error = str(exc)
            try:
                signals.checked.emit(info, error, manual)
            except RuntimeError:
                pass  # The owning window may have closed during a request.

        threading.Thread(target=check, name="MotuNet-update-check", daemon=True).start()

    def update_checked(self, info, error, manual):
        self.update_checking = False
        if self.updates_closed:
            return
        self.update_button.setEnabled(not self.update_busy)
        self.update_button.setText("检查更新")
        if error:
            self.append_log("检查更新未完成：" + error)
            if manual:
                self.update_message("检查更新未完成", "暂时无法获取更新，请检查 GitHub 是否可访问后重试。\n\n" + error)
            return
        if info is None:
            if manual:
                self.update_message("检查更新", f"当前已是最新版本：{__version__}")
            return
        self.update_info = info
        self.pending_update_notice = info
        self.update_button.setText(f"发现新版 {info.version}")
        self.update_notice_timer.start()
        if manual or not (self.busy or self.session_active):
            self.deliver_update_notice(force=manual)
        else:
            # An automatic check must never interrupt an active network test.
            self.append_log(f"发现新版 {info.version}，测试结束后提示更新。")

    def update_message(self, title, message):
        if self.update_dialog is not None:
            self.update_dialog.close()
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(message)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setStyleSheet(stylesheet(self.theme_id))
        self.update_dialog = box
        box.finished.connect(lambda *_: self.clear_update_dialog(box))
        box.open()

    def clear_update_dialog(self, box):
        if self.update_dialog is box:
            self.update_dialog = None

    def deliver_update_notice(self, force=False):
        if self.updates_closed or self.update_busy or self.busy:
            return
        if self.session_active and not force:
            return
        info = self.pending_update_notice
        if info is None:
            self.update_notice_timer.stop()
            return
        self.pending_update_notice = None
        self.update_notice_timer.stop()
        if self.update_dialog is not None:
            self.update_dialog.close()
        box = QMessageBox(self)
        box.setWindowTitle("发现新版本")
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"Motu Net {info.version} 已发布，当前版本为 {__version__}。")
        box.setInformativeText((info.notes[:3000] or "功能与稳定性更新。") +
            "\n\n确认后将停止当前测试并恢复网络，下载新版并打开安装向导。场景和皮肤设置会保留。")
        accept = box.addButton("下载并更新", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        box.setStyleSheet(stylesheet(self.theme_id))
        self.update_dialog = box

        def finished(_):
            self.clear_update_dialog(box)
            if box.clickedButton() is accept:
                self.start_update(info)

        box.finished.connect(finished)
        box.open()

    def start_update(self, info):
        if self.updates_closed or self.busy or self.update_busy:
            return
        self.update_info = info
        self.update_cancel.clear()
        self.update_busy = True
        self.update_button.setEnabled(False)
        self.set_running_controls()
        # Always complete normal engine shutdown before making a download.
        self.job("update_stop", self.engine.stop)

    def begin_update_download(self):
        if self.close_pending or self.updates_closed:
            self.update_busy = False
            self.close()
            return
        dialog = QProgressDialog("正在下载更新…", "取消", 0, 100, self)
        dialog.setWindowTitle(f"更新到 {self.update_info.version}")
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(self.update_cancel.set)
        dialog.setStyleSheet(stylesheet(self.theme_id))
        self.update_progress_dialog = dialog
        dialog.show()
        signals, cancel = self.update_signals, self.update_cancel
        client, info = self.update_client, self.update_info
        directory = Path(self.store.root) / "updates"

        def download():
            path, error, cancelled = None, "", False
            try:
                path = client.download(info, directory, signals.progress.emit, cancel)
            except UpdateCancelled:
                cancelled = True
            except Exception as exc:
                error = str(exc)
            try:
                signals.downloaded.emit(path, error, cancelled)
            except RuntimeError:
                pass

        threading.Thread(target=download, name="MotuNet-update-download", daemon=True).start()

    def update_download_progress(self, received, total):
        dialog = self.update_progress_dialog
        if dialog is not None and not self.update_cancel.is_set():
            dialog.setLabelText(f"正在下载更新：{received / 1048576:.1f} / {total / 1048576:.1f} MB")
            # A modal progress dialog can process queued completion events in
            # setValue(), so do not access the shared dialog reference afterward.
            dialog.setValue(min(100, received * 100 // max(1, total)))

    def update_downloaded(self, path, error, cancelled):
        # Closing a QProgressDialog can emit canceled: capture intent first.
        cancelled = cancelled or self.update_cancel.is_set()
        if self.update_progress_dialog is not None:
            self.update_progress_dialog.blockSignals(True)
            self.update_progress_dialog.close()
            self.update_progress_dialog = None
        self.update_busy = False
        if self.updates_closed:
            return
        self.update_button.setEnabled(True)
        self.set_running_controls()
        if self.close_pending:
            self.close()
            return
        if cancelled:
            self.append_log("已取消更新下载。")
            return
        if error:
            self.update_message("更新下载失败", error + "\n\n当前版本仍可继续使用，请稍后重试。")
            return
        try:
            launch_installer(Path(path))
        except OSError as exc:
            self.update_message("安装未启动", str(exc) + "\n\n当前版本仍可继续使用。")
            return
        self.append_log("已打开新版安装向导，正在退出当前程序。")
        self.close()

    def close_updates(self):
        self.updates_closed = True
        self.update_cancel.set()
        self.update_start_timer.stop()
        self.update_notice_timer.stop()
