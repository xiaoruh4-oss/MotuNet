"""Exercise the actual Inno driver check in an isolated, non-installing fixture.

Creates no uninstall entry, registry entries, shortcuts or driver services.
The only destination is a unique directory under artifacts/installer-driver.
Locked changed-driver replacement is deliberately not attempted: it would
register an OS-wide pending reboot operation.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DRIVER_RELATIVE = Path("_internal/vendor/windivert/WinDivert64.sys")


def compiler_path(requested: str | None) -> Path:
    candidates = [requested] if requested else [
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "MutuNetBuildTools/InnoSetup/ISCC.exe"),
        str(Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6/ISCC.exe"),
        str(Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6/ISCC.exe"),
        shutil.which("ISCC.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise RuntimeError("Inno Setup compiler not found")


def lock_readonly(path: Path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Permit read/hash while denying all writes and rename/delete replacement.
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def run_fixture(compiler: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("This fixture requires Windows")
    base = ROOT / "artifacts/installer-driver"
    base.mkdir(parents=True, exist_ok=True)
    case_root = Path(tempfile.mkdtemp(prefix="check-", dir=base)).resolve()
    source = (ROOT / "packaging/MotuNet.iss").read_text(encoding="utf-8")
    definition = "\n".join(line for line in source.splitlines() if line.startswith((
        "#define DriverRelativePath ", "#define BundledDriverSHA256 ")))
    function = source[source.index("function ShouldInstallDriver:"):source.index("function NormalizeInstallDir(")]
    entry = next(line for line in source.splitlines() if line.startswith('Source: "{#PayloadDir}\\{#DriverRelativePath}"'))
    wildcard_entry = next(line for line in source.splitlines() if line.startswith('Source: "{#PayloadDir}\\_internal\\*"'))
    if len(definition.splitlines()) != 2 or "Check: ShouldInstallDriver" not in entry:
        raise RuntimeError("Expected driver hash definitions/check are missing")
    payload = case_root / "payload"
    bundled = payload / DRIVER_RELATIVE
    bundled.parent.mkdir(parents=True)
    # Harmless fixture data, never a real or loadable Windows driver.
    content = b"MotuNet driver replacement regression fixture v1\n"
    bundled.write_bytes(content)
    script = f'''#define PayloadDir "{payload}"
{definition}
[Setup]
AppName=MotuNet isolated driver fixture
AppVersion=0.0.0
DefaultDirName={case_root / "unused"}
UsePreviousAppDir=no
CreateAppDir=yes
PrivilegesRequired=lowest
Uninstallable=no
CreateUninstallRegKey=no
UpdateUninstallLogAppName=no
DisableDirPage=yes
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
OutputDir={case_root}
OutputBaseFilename=driver-fixture
Compression=none
DiskSpanning=no
[Files]
{wildcard_entry}
{entry}
[Code]
{function}
'''
    fixture = case_root / "driver-fixture.iss"
    fixture.write_text(script, encoding="utf-8")
    compiled = subprocess.run([str(compiler), str(fixture)], capture_output=True, text=True)
    (case_root / "compile.log").write_text(compiled.stdout + compiled.stderr, encoding="utf-8")
    if compiled.returncode:
        raise RuntimeError(f"Fixture compilation failed; see {case_root / 'compile.log'}")
    report = {"fixture_directory": str(case_root), "cases": [],
              "limitations": ["No real installation, driver load, registry or pending-reboot write",
                              "Changed-and-locked driver restart behavior retained, not executed"]}
    for name in ("missing", "changed_unlocked", "identical_unlocked", "identical_locked"):
        destination = case_root / name
        destination.mkdir()
        target = destination / DRIVER_RELATIVE
        target.parent.mkdir(parents=True)
        if name.startswith("identical"):
            target.write_bytes(content)
        elif name == "changed_unlocked":
            target.write_bytes(b"older harmless fixture data")
        if target.exists():
            os.utime(target, ns=(946684800000000000, 946684800000000000))
        before = target.stat().st_mtime_ns if target.exists() else None
        kernel = handle = None
        if name == "identical_locked":
            kernel, handle = lock_readonly(target)
        try:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            log = case_root / f"{name}.log"
            result = subprocess.run([str(case_root / "driver-fixture.exe"),
                "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RESTARTEXITCODE=77",
                f"/DIR={destination}", f"/LOG={log}"], startupinfo=startup, timeout=45)
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
        log_text = log.read_text(encoding="utf-8-sig", errors="replace")
        if result.returncode != 0:
            raise AssertionError(f"{name}: setup exit {result.returncode}; see {log}")
        if target.read_bytes() != content:
            raise AssertionError(f"{name}: wrong final file content")
        same = name.startswith("identical")
        skipped = "matches bundled SHA256; skipping driver replacement" in log_text
        if skipped != same:
            raise AssertionError(f"{name}: unexpected skip result")
        if same and target.stat().st_mtime_ns != before:
            raise AssertionError(f"{name}: identical driver was modified")
        if not re.search(r"Need to restart Windows\? No", log_text, re.I):
            raise AssertionError(f"{name}: expected explicit no-restart result in log")
        report["cases"].append({"case": name, "exit_code": result.returncode,
            "identical_file_skipped": skipped, "restart_required": False,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    report["passed"] = True
    (case_root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler")
    arguments = parser.parse_args()
    print(json.dumps(run_fixture(compiler_path(arguments.compiler)), indent=2))
