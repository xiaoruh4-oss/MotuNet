"""Windows application integration; importing this module never changes traffic."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys


def resource_dir():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def is_admin():
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def elevate():
    if getattr(sys, "frozen", False):
        executable, args = sys.executable, []
    else:
        executable = sys.executable
        args = [str(resource_dir() / "main.py")]
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.restype = ctypes.c_ssize_t
    result = shell(None, "runas", executable, subprocess.list2cmdline(args),
                   str(resource_dir()), 1)
    if result <= 32:
        raise OSError(
            f"管理员重启未完成（代码 {result}）。请在 Windows 安全提示中选择“是”，"
            "或右键 Motu Net 选择“以管理员身份运行”。"
        )


def launch_installer(path):
    """Hand a verified installer to Windows; the installer requests elevation."""
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != ".exe" or os.name != "nt":
        raise OSError("当前平台无法运行 Windows 安装程序")
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
    shell.restype = ctypes.c_ssize_t
    result = shell(None, "open", str(path), None, str(path.parent), 1)
    if result <= 32:
        raise OSError(f"安装程序未启动或管理员授权已取消（代码 {result}）")


class InstanceLock:
    """File lock shared across elevated/non-elevated sessions; released on exit."""
    def __init__(self, root):
        self.file = None
        self.root = Path(root)

    def acquire(self):
        import msvcrt
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = (self.root / "instance.lock").open("a+b")
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def release(self):
        if self.file:
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None
