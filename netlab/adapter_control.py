"""Bounded Windows adapter operations, addressed by validated interface GUIDs."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET


def canonical_guid(value):
    if not isinstance(value, str):
        raise ValueError("网卡标识必须是 GUID")
    try:
        return str(uuid.UUID(value.strip().strip("{}"))).upper()
    except (ValueError, AttributeError) as exc:
        raise ValueError("网卡标识无效，请重新选择网卡") from exc


def canonical_ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 16:
        raise ValueError("请至少选择一张网卡（最多 16 张）")
    ids = [canonical_guid(value) for value in values]
    if len(ids) != len(set(ids)):
        raise ValueError("不能重复选择同一网卡")
    return ids


def _encoded(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell():
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) /
               "System32/WindowsPowerShell/v1.0/powershell.exe")


def _powershell_error(stderr):
    """Extract the first readable error from Windows PowerShell's CLIXML."""
    detail = stderr.decode("utf-8", "replace").strip()
    if detail.startswith("#< CLIXML"):
        try:
            xml = ET.fromstring(detail.split("\n", 1)[1].strip())
            for item in xml.iter():
                if item.tag.rsplit("}", 1)[-1] == "S" and item.get("S") == "Error" and item.text:
                    # Decode each CLIXML escape once, preserving escaped literal
                    # strings such as _x005F_x000D_ rather than decoding twice.
                    value = re.sub(r"_x([0-9A-Fa-f]{4})_",
                                   lambda match: chr(int(match.group(1), 16)), item.text).strip()
                    if value:
                        return value[:1600]
        except (ET.ParseError, IndexError, ValueError):
            pass
    return detail[:1600]


def run_powershell(script, *, timeout=20):
    if os.name != "nt":
        raise OSError("网卡断网仅支持 Windows")
    script = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);\n" + script)
    try:
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", _encoded(script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Windows 网卡操作超时；恢复保护仍保留") from exc
    if result.returncode:
        raise OSError("Windows 网卡操作失败：" + _powershell_error(result.stderr))
    return result.stdout.decode("utf-8-sig").strip()


def _selector(ids):
    # Only canonical UUIDs enter command text; display names never become code.
    literals = ",".join("[Guid]'" + value + "'" for value in canonical_ids(ids))
    return "$ids=@(" + literals + "); $adapters=@(Get-NetAdapter -IncludeHidden | Where-Object { $ids -contains [Guid]$_.InterfaceGuid });\n"


_ROWS = """
@($adapters | ForEach-Object {
    [pscustomobject]@{guid=([Guid]$_.InterfaceGuid).ToString().ToUpperInvariant();name=$_.Name;
    description=$_.InterfaceDescription;admin_up=([int]$_.AdminStatus -eq 1);
    status=[string]$_.Status;connected=([string]$_.Status -eq 'Up');physical=[bool]$_.HardwareInterface;
    hidden=[bool]$_.Hidden}
}) | ConvertTo-Json -Compress -Depth 3
"""


def _rows(text):
    if not text:
        return []
    value = json.loads(text)
    return value if isinstance(value, list) else [value]


def list_adapters():
    """Read physical adapters without enabling or disabling anything."""
    return _rows(run_powershell("$adapters=@(Get-NetAdapter | Where-Object { $_.HardwareInterface -and "
                               "-not $_.Hidden -and [string]$_.Status -notin @('Not Present','Unknown') });\n" + _ROWS))


class WindowsAdapterControl:
    def inspect(self, ids):
        return _rows(run_powershell(_selector(ids) + _ROWS))

    def preflight(self, ids):
        ids = canonical_ids(ids)
        rows = self.inspect(ids)
        by_id = {row["guid"]: row for row in rows}
        for guid in ids:
            row = by_id.get(guid)
            if not row or not row.get("physical") or row.get("status") in ("Not Present", "Unknown"):
                raise ValueError("所选物理网卡已不存在，请刷新网卡列表")
            if row.get("hidden"):
                raise ValueError("所选网卡是系统隐藏接口，无法用于断网测试，请刷新并选择可见物理网卡")
            if not row.get("admin_up"):
                raise ValueError("所选网卡原本已禁用，不会接管或自动启用：" + row["name"])
        return [by_id[guid] for guid in ids]

    def disable(self, ids):
        script = _selector(ids) + """
if ($adapters.Count -ne $ids.Count) { throw 'Selected adapter disappeared' }
foreach ($a in $adapters) {
    if (-not $a.HardwareInterface -or $a.Hidden -or [string]$a.Status -in @('Not Present','Unknown') -or [int]$a.AdminStatus -ne 1) { throw 'Adapter is not an enabled visible physical adapter' }
}
foreach ($a in $adapters) { $a | Disable-NetAdapter -Confirm:$false -ErrorAction Stop }
""" + _selector(ids) + """
if ($adapters.Count -ne $ids.Count -or @($adapters | Where-Object { [int]$_.AdminStatus -ne 2 }).Count) {
    throw 'Cannot confirm all selected adapters are disabled'
}
"""
        run_powershell(script)

    def restore(self, ids):
        script = _selector(ids) + """
if ($adapters.Count -ne $ids.Count) { throw 'Selected adapter unavailable; recovery remains pending' }
foreach ($a in $adapters) {
    if (-not $a.HardwareInterface) { throw 'Selected adapter is no longer a physical device' }
    if ([int]$a.AdminStatus -ne 1) { $a | Enable-NetAdapter -Confirm:$false -ErrorAction Stop }
}
""" + _selector(ids) + """
if ($adapters.Count -ne $ids.Count -or @($adapters | Where-Object { [int]$_.AdminStatus -ne 1 }).Count) {
    throw 'Adapter enable state has not been confirmed'
}
"""
        run_powershell(script)

    def register_recovery(self, task, ids, seconds):
        task = validated_task_name(task)
        if not isinstance(seconds, int) or not 1 <= seconds <= 360:
            raise ValueError("恢复任务期限无效")
        # The task embeds only UUIDs and fixed commands, never a user-writable script path.
        # SYSTEM can recover even if the desktop process exits or the user signs out.
        body = "$ErrorActionPreference='Stop';\n" + """
$complete=$false
for ($attempt=0; $attempt -lt 18; $attempt++) {
  try {
""" + _selector(ids) + """
    if ($adapters.Count -ne $ids.Count) { throw 'Adapter missing' }
    foreach ($a in $adapters) {
      if (-not $a.HardwareInterface) { throw 'Not a physical adapter' }
      if ([int]$a.AdminStatus -ne 1) { $a | Enable-NetAdapter -Confirm:$false -ErrorAction Stop }
    }
""" + _selector(ids) + """
    if (@($adapters | Where-Object { [int]$_.AdminStatus -ne 1 }).Count -eq 0) { $complete=$true; break }
  } catch { }
  Start-Sleep -Seconds 3
}
""" + f"if ($complete) {{ Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue }}\n"
        encoded = _encoded(body)
        script = rf"""
$exe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action=New-ScheduledTaskAction -Execute $exe -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}'
$trigger=New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds({seconds})
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
$null=Register-ScheduledTask -TaskName '{task}' -Action $action -Trigger $trigger -Principal $principal -Settings $settings
$registered=Get-ScheduledTask -TaskName '{task}' -ErrorAction Stop
if ($registered.Principal.UserId -notin @('SYSTEM','S-1-5-18')) {{ throw 'Recovery task registration not confirmed' }}
"""
        run_powershell(script)

    def remove_recovery(self, task):
        task = validated_task_name(task)
        # Enumerating then filtering treats an absent task as successful cleanup,
        # while query/access errors and real unregister failures still terminate.
        run_powershell(f"""
$existing=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {{ $_.TaskName -eq '{task}' -and $_.TaskPath -eq '\\' }})
if ($existing.Count) {{
    $existing | Unregister-ScheduledTask -Confirm:$false -ErrorAction Stop
    $remaining=@(Get-ScheduledTask -ErrorAction Stop | Where-Object {{ $_.TaskName -eq '{task}' -and $_.TaskPath -eq '\\' }})
    if ($remaining.Count) {{ throw 'Recovery task removal not confirmed' }}
}}
exit 0
""")

    def pending_tasks(self):
        text = run_powershell("@(Get-ScheduledTask -TaskName 'MotuNet-Restore-*' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TaskName) | ConvertTo-Json -Compress")
        return _rows(text)


def validated_task_name(task):
    prefix = "MotuNet-Restore-"
    if not isinstance(task, str) or not task.startswith(prefix):
        raise ValueError("恢复任务标识无效")
    suffix = task[len(prefix):]
    if len(suffix) != 32 or uuid.UUID(suffix).hex != suffix:
        raise ValueError("恢复任务标识无效")
    return task


class ProcessWatch:
    """An open process handle avoids PID reuse while watching the desktop owner."""
    def __init__(self, pid):
        if os.name != "nt":
            raise OSError("网卡恢复监控仅支持 Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE

    def alive(self):
        return bool(self.handle and self.kernel.WaitForSingleObject(self.handle, 0) == 258)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def process_alive(pid):
    watch = ProcessWatch(pid)
    try:
        return watch.alive()
    finally:
        watch.close()


class AdapterSessionLock:
    """A machine-wide mutex prevents simultaneous adapter outages by this tool."""
    def __enter__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, "Global\\MotuNet.AdapterOutage")
        if not self.handle:
            raise OSError("无法建立网卡恢复锁，请以管理员身份运行")
        result = self.kernel.WaitForSingleObject(self.handle, 0)
        if result not in (0, 0x80):
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError("另一个 Motu Net 正在操作网卡，请先停止并恢复")
        return self

    def __exit__(self, *_):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
