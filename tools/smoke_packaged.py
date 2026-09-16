"""Launch the packaged app with isolated data, then close its own hidden window."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from netlab import __version__
user32 = ctypes.WinDLL("user32", use_last_error=True)
callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def app_window(pid):
    found = []

    @callback_type
    def visit(hwnd, unused):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value.startswith("Motu Net v"):
                found.append((hwnd, title.value))
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


report = {}
with tempfile.TemporaryDirectory(prefix="motu-release-smoke-") as temporary:
    env = dict(os.environ, LOCALAPPDATA=temporary, QT_QPA_PLATFORM="windows")
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    process = subprocess.Popen([str(ROOT / "staging" / "MotuNet" / "MotuNet.exe")],
                               env=env, startupinfo=info)
    try:
        window = None
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            window = app_window(process.pid)
            if window:
                break
            time.sleep(.1)
        if window is None:
            raise RuntimeError("Packaged Motu Net window did not initialize")
        hwnd, title = window
        report["window_title"] = title
        report["started"] = True
        # This handle was checked against the exact child PID above. Nothing
        # belonging to an existing user instance is inspected or closed.
        if not user32.PostMessageW(hwnd, 0x0010, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        report["exit_code"] = process.wait(timeout=10)
        events = []
        for log in (Path(temporary) / "MotuNet" / "audit").glob("*.jsonl"):
            events.extend(json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
        report["events"] = [event["event"] for event in events]
        report["passed"] = (report["exit_code"] == 0 and title == f"Motu Net v{__version__}"
                            and report["events"] == ["app_open", "app_close"])
        if not report["passed"]:
            raise RuntimeError("Packaged application lifecycle failed")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        report["test_process_exited"] = process.poll() is not None
        (ROOT / "artifacts" / f"packaged-smoke-{__version__}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
