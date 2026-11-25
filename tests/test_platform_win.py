import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from netlab import platform_win


class ElevationCommandTests(unittest.TestCase):
    def test_source_command_uses_actual_script_and_forwards_arguments(self):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "main.py"
            script.write_text("", encoding="utf-8")
            with patch.object(sys, "frozen", False, create=True), \
                    patch.object(sys, "argv", [str(script), "--local-test"]), \
                    patch.object(sys, "executable", sys.executable):
                executable, args, working_dir = platform_win._elevation_command()

        self.assertEqual(Path(executable), Path(sys.executable).resolve())
        self.assertEqual(args, [str(script.resolve()), "--local-test"])
        self.assertEqual(working_dir, script.parent.resolve())

    def test_frozen_command_uses_executable_directory_and_forwards_arguments(self):
        frozen_executable = r"C:\Apps\Motu Net\MotuNet.exe"
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "argv", ["MotuNet.exe", "--local-test"]), \
                patch.object(sys, "executable", frozen_executable):
            executable, args, working_dir = platform_win._elevation_command()

        self.assertEqual(executable, str(Path(frozen_executable).resolve()))
        self.assertEqual(args, ["--local-test"])
        self.assertEqual(working_dir, Path(frozen_executable).resolve().parent)

    @patch("netlab.platform_win.ctypes.windll.shell32")
    def test_elevate_uses_runas_and_returns_on_success(self, shell32):
        shell32.ShellExecuteW.return_value = 33
        with patch.object(platform_win, "_elevation_command", return_value=(
                r"C:\Apps\Motu Net\MotuNet.exe", ["--local-test"],
                Path(r"C:\Apps\Motu Net"))):
            platform_win.elevate()

        call = shell32.ShellExecuteW.call_args
        self.assertEqual(call.args[1], "runas")
        self.assertEqual(call.args[2], r"C:\Apps\Motu Net\MotuNet.exe")
        self.assertIn("--local-test", call.args[3])
        self.assertEqual(call.args[4], r"C:\Apps\Motu Net")

    @patch("netlab.platform_win.ctypes.windll.shell32")
    def test_elevate_reports_uac_cancellation(self, shell32):
        shell32.ShellExecuteW.return_value = 5
        with patch.object(platform_win, "_elevation_command", return_value=(
                r"C:\Apps\Motu Net\MotuNet.exe", [],
                Path(r"C:\Apps\Motu Net"))):
            with self.assertRaisesRegex(OSError, "代码 5"):
                platform_win.elevate()


if __name__ == "__main__":
    unittest.main()
