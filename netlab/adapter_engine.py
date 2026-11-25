"""Independent adapter-outage worker with persisted and scheduled recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .adapter_control import (AdapterSessionLock, ProcessWatch, WindowsAdapterControl,
                              canonical_ids, process_alive, validated_task_name)
from .engine import Engine
from .storage import data_dir


def validate_adapter_config(config):
    cfg = dict(config)
    if cfg.get("blackout") is not True or cfg.get("blackout_mode") != "adapter":
        raise ValueError("网卡断网需要启用系统网卡模式")
    cfg["adapter_ids"] = canonical_ids(cfg.get("adapter_ids"))
    for key, required in (("scope", "all"), ("protocol", "all"), ("direction", "both")):
        if cfg.get(key, required) != required:
            raise ValueError("禁用网卡影响整张网卡，不能按 IP、端口、协议或方向过滤")
    if any(cfg.get(key) for key in ("host", "ports", "advanced_filter")):
        raise ValueError("网卡断网不能包含流量过滤目标")
    duration = cfg.get("blackout_duration_s", 0)
    delay = cfg.get("start_delay_s", 0)
    if type(duration) is not int or not 1 <= duration <= 300:
        raise ValueError("系统断网必须在 1 至 300 秒后自动恢复")
    if type(delay) is not int or not 0 <= delay <= 86400:
        raise ValueError("延后断网时间必须为 0 至 86400 整数秒")
    if cfg.get("blackout_loop"):
        raise ValueError("系统网卡断网暂不支持循环执行")
    return cfg


def recovery_root():
    return data_dir() / "adapter-recovery"


def _write(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with tmp.open("w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        # Windows readers briefly open the destination without FILE_SHARE_DELETE.
        # Keep the atomic replacement, retrying only transient access collisions
        # for at most 20 short waits (0.5 seconds total).
        for attempt in range(21):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 20:
                    raise
                time.sleep(.025)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read(path):
    path = Path(path)
    if path.stat().st_size > 128_000:
        raise ValueError("网卡恢复记录过大")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("网卡恢复记录无效")
    return value


def _initial():
    return dict(Engine._new_stats(), mode="adapter", adapter_names=[], adapter_disabled=False,
                adapter_restored=False, adapter_attempted=False, recovery_pending=False, recovery_task="")


def run_session(directory, control, parent, *, clock=time.monotonic, sleep=time.sleep):
    """Core worker lifecycle; injectable clock/control exercise failure paths safely."""
    directory = Path(directory)
    request = _read(directory / "request.json")
    cfg = validate_adapter_config(request["config"])
    ids = cfg["adapter_ids"]
    task = "MotuNet-Restore-" + directory.name
    validated_task_name(task)
    started = clock()
    state = _initial()
    state.update(running=True, phase="starting", worker_pid=os.getpid(), cycle=1,
                 adapter_ids=ids, recovery_task=task)
    journal = dict(task=task, adapter_ids=ids, attempted=False, restored=False,
                   worker_pid=os.getpid(), created_at=time.time())
    armed = False
    attempted = False
    active_at = None
    failure = None

    def status(**updates):
        state.update(updates)
        state["elapsed"] = max(0, clock() - started)
        if active_at is not None:
            if state.get("phase") == "active":
                state["active_elapsed"] = max(0, clock() - active_at)
                state["active_remaining"] = max(0, cfg["blackout_duration_s"] - state["active_elapsed"])
            else:
                state["active_remaining"] = None
        _write(directory / "status.json", state)

    def cancelled():
        return (directory / "stop").exists() or not parent.alive()

    try:
        rows = control.preflight(ids)
        state["adapter_names"] = [row["name"] for row in rows]
        journal["adapter_names"] = list(state["adapter_names"])
        journal["originally_enabled"] = [row["guid"] for row in rows if row.get("admin_up")]
        _write(directory / "recovery.json", journal)
        if control.pending_tasks():
            raise RuntimeError("发现尚未完成的网卡恢复任务，请先恢复网络或等待兜底恢复完成")
        wait_until = clock() + cfg["start_delay_s"]
        while clock() < wait_until and not cancelled():
            status(phase="waiting", wait_remaining=max(0, wait_until - clock()))
            sleep(min(.2, max(0, wait_until - clock())))
        if cancelled():
            status(running=False, phase="stopped", adapter_restored=True, wait_remaining=0)
            return state
        # Recheck after the delay. Originally disabled adapters are never claimed.
        control.preflight(ids)
        # Keep at least 45 seconds beyond the selected outage for bounded OS calls.
        # Task registration precedes mutation; failure therefore prevents an outage.
        # Treat an attempted registration as armed: a timeout may happen after
        # Task Scheduler already saved it. Cleanup is required even on failure.
        armed = True
        journal["registration_requested"] = True
        _write(directory / "recovery.json", journal)
        control.register_recovery(task, ids, cfg["blackout_duration_s"] + 45)
        journal["armed_at"] = time.time()
        _write(directory / "recovery.json", journal)
        if cancelled():
            return state
        attempted = True
        journal["attempted"] = True
        _write(directory / "recovery.json", journal)
        status(phase="disabling", wait_remaining=0, recovery_pending=True, adapter_attempted=True)
        control.disable(ids)
        active_at = clock()
        status(phase="active", adapter_disabled=True)
        until = active_at + cfg["blackout_duration_s"]
        while clock() < until and not cancelled():
            status(phase="active")
            sleep(min(.2, max(0, until - clock())))
    except Exception as exc:
        failure = str(exc)
        state.update(error=failure, errors=1)
    finally:
        restored = not attempted
        if attempted:
            # Do not abandon restoration when the parent is gone or stop requested.
            # Retry transient device-enumeration errors while the scheduled fallback stays armed.
            for attempt in range(3):
                try:
                    status(phase="restoring", recovery_pending=True)
                except OSError:
                    pass
                try:
                    control.restore(ids)
                    restored = True
                    break
                except Exception as exc:
                    failure = "网卡恢复尚未确认：" + str(exc)
                    state.update(error=failure, errors=state.get("errors", 0) + 1)
                    sleep(.5)
        if restored:
            journal["restored"] = True
            journal["finished_at"] = time.time()
            try:
                _write(directory / "recovery.json", journal)
            except OSError as exc:
                failure = failure or "网卡已启用，但恢复记录保存失败：" + str(exc)
            if armed:
                try:
                    control.remove_recovery(task)
                    journal["task_removed"] = True
                    _write(directory / "recovery.json", journal)
                except Exception as exc:
                    # Network restoration is true even if task cleanup fails.
                    state["recovery_cleanup_warning"] = str(exc)
            state.update(running=False, phase="error" if failure else "stopped",
                         adapter_disabled=False, adapter_restored=True, recovery_pending=False,
                         wait_remaining=0, active_remaining=None, error=failure)
        else:
            state.update(running=True, phase="restore_failed", error=failure,
                         recovery_pending=True, adapter_restored=False)
        try:
            status()
        except OSError:
            pass
    return state


def worker_main(directory):
    """Call before Qt/InstanceLock initialization for --adapter-worker."""
    directory = Path(directory).resolve()
    # Only our UUID session folders and their bounded request data are accepted.
    uuid.UUID(directory.name)
    request = _read(directory / "request.json")
    parent = ProcessWatch(int(request["parent_pid"]))
    try:
        with AdapterSessionLock():
            state = run_session(directory, WindowsAdapterControl(), parent)
        return 0 if state.get("adapter_restored") and not state.get("error") else 1
    except Exception as exc:
        try:
            previous = _read(directory / "status.json")
        except (OSError, ValueError):
            previous = _initial()
        pending = directory in pending_recoveries(directory.parent)
        previous.update(running=pending, phase="restore_failed" if pending else "error",
                        recovery_pending=pending, error=str(exc), errors=1)
        _write(directory / "status.json", previous)
        return 1
    finally:
        parent.close()


def recover_session(directory, *, control=None):
    """Restore a journal belonging to a worker that has already exited."""
    directory = Path(directory)
    journal = _read(directory / "recovery.json")
    task = validated_task_name(journal["task"])
    ids = canonical_ids(journal["adapter_ids"])
    if journal.get("attempted"):
        originally_enabled = canonical_ids(journal.get("originally_enabled", []))
        if not set(ids).issubset(originally_enabled):
            raise ValueError("恢复记录未确认网卡原先启用，请在 Windows 设置中手动检查")
    control = control or WindowsAdapterControl()
    if journal.get("worker_pid") and process_alive(journal["worker_pid"]):
        raise RuntimeError("网卡恢复进程仍在运行，请等待它完成")
    if journal.get("attempted") and not journal.get("restored"):
        control.restore(ids)
    journal["restored"] = True
    journal["finished_at"] = time.time()
    _write(directory / "recovery.json", journal)
    try:
        state = _read(directory / "status.json")
    except (OSError, ValueError):
        state = _initial()
    state.update(running=True, phase="restoring", adapter_restored=True, adapter_disabled=False,
                 adapter_attempted=bool(journal.get("attempted")), recovery_pending=True)
    _write(directory / "status.json", state)
    control.remove_recovery(task)
    journal["task_removed"] = True
    _write(directory / "recovery.json", journal)
    state.update(running=False, phase="stopped", adapter_restored=True,
                 adapter_disabled=False, recovery_pending=False, error=None,
                 wait_remaining=0, active_remaining=None)
    _write(directory / "status.json", state)
    return state


def recover_pending(root=None):
    """Run off the GUI thread with administrator rights; returns recovery errors."""
    errors = []
    root = Path(root) if root else recovery_root()
    if not root.exists():
        return errors
    with AdapterSessionLock():
        for directory in root.iterdir():
            if not directory.is_dir() or not (directory / "recovery.json").exists():
                continue
            try:
                uuid.UUID(directory.name)
                journal = _read(directory / "recovery.json")
                if journal.get("restored") and journal.get("task_removed"):
                    continue
                if journal.get("worker_pid") and process_alive(journal["worker_pid"]):
                    continue
                recover_session(directory)
            except Exception as exc:
                errors.append(str(exc))
    return errors


def pending_recoveries(root=None):
    """Read journal paths only; no PowerShell, network or adapter changes."""
    root = Path(root) if root else recovery_root()
    if not root.exists():
        return []
    pending = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / "recovery.json").exists():
            continue
        try:
            uuid.UUID(directory.name)
            journal = _read(directory / "recovery.json")
            if journal.get("restored") and (journal.get("task_removed")
                    or not journal.get("registration_requested")):
                continue
            if journal.get("attempted") or journal.get("registration_requested"):
                pending.append(directory)
        except (OSError, ValueError, KeyError):
            # Keep malformed recovery records visible so a fresh outage cannot
            # silently proceed while an earlier result remains unknown.
            pending.append(directory)
    return pending


class AdapterEngine:
    def __init__(self, *, root=None):
        self.root = Path(root) if root else recovery_root()
        self.directory = None
        self.process = None
        self._state = _initial()
        self._lock = threading.RLock()

    def adopt_pending(self):
        """Attach the oldest unresolved journal for recovery after a restart."""
        pending = pending_recoveries(self.root)
        if not pending:
            return False
        with self._lock:
            self.directory = pending[0]
            self.process = None
            try:
                self._state = _read(self.directory / "status.json")
            except (OSError, ValueError):
                self._state = _initial()
            try:
                journal = _read(self.directory / "recovery.json")
                self._state.update(adapter_ids=journal.get("adapter_ids", []),
                                   adapter_names=journal.get("adapter_names", []),
                                   worker_pid=journal.get("worker_pid"))
            except (OSError, ValueError):
                pass
            self._state.update(running=True, phase="restore_failed", recovery_pending=True,
                               error="发现上次未确认完成的网卡恢复，请点击停止并恢复")
        return True

    def validate_filter(self, expression):
        return expression  # System adapter mode does not use WinDivert.

    def start(self, config):
        cfg = validate_adapter_config(config)
        with self._lock:
            if self.snapshot().get("running"):
                raise RuntimeError("网卡断网仍在运行或恢复中")
            if pending_recoveries(self.root):
                raise RuntimeError("存在尚未确认的网卡恢复记录，请先停止并恢复")
            self.root.mkdir(parents=True, exist_ok=True)
            self.directory = self.root / uuid.uuid4().hex
            self.directory.mkdir()
            _write(self.directory / "request.json", dict(config=cfg, parent_pid=os.getpid()))
            self._state = dict(_initial(), running=True, phase="starting")
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--adapter-worker", str(self.directory)]
            else:
                command = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "main.py"),
                           "--adapter-worker", str(self.directory)]
            try:
                self.process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            except Exception:
                self._state = _initial()
                raise
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            state = self.snapshot()
            if state.get("phase") in ("waiting", "active"):
                return
            if not state.get("running") or state.get("phase") == "restore_failed":
                if state.get("error"):
                    raise RuntimeError(state["error"])
                return
            time.sleep(.1)
        (self.directory / "stop").touch()
        raise TimeoutError("网卡启动响应超时，已请求恢复，请查看恢复状态")

    def snapshot(self):
        with self._lock:
            if self.directory:
                try:
                    self._state = _read(self.directory / "status.json")
                except (OSError, ValueError):
                    pass
                pending = pending_recoveries(self.root)
                exited = self.process is not None and self.process.poll() is not None
                if self.directory in pending and (exited or self.process is None or not self._state.get("running")):
                    self._state.update(running=True, phase="restore_failed", recovery_pending=True,
                                       error="网卡恢复或恢复任务清理尚未确认，请点击停止并恢复重试")
                elif exited and self._state.get("running"):
                    # The worker persists its recovery journal before registering
                    # a fallback or disabling any adapter. An early process exit
                    # with no journal therefore has no recovery responsibility.
                    # An existing but unreadable/malformed journal remains pending.
                    try:
                        (self.directory / "recovery.json").stat()
                    except FileNotFoundError:
                        self._state.update(running=False, phase="error", recovery_pending=False,
                                           adapter_attempted=False, adapter_disabled=False,
                                           error="网卡断网进程启动失败，尚未执行禁用网卡")
                    except OSError:
                        self._state.update(phase="restore_failed", recovery_pending=True,
                                           error="无法确认网卡恢复记录，请点击停止并恢复检查")
                    else:
                        self._state.update(phase="restore_failed", recovery_pending=True,
                                           error="网卡恢复进程已退出，请点击停止并恢复检查恢复记录")
                if not self._state.get("running") and pending:
                    self.adopt_pending()
            return dict(self._state)

    def stop(self):
        with self._lock:
            if not self.directory and not self.adopt_pending():
                return
            (self.directory / "stop").touch()
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            state = self.snapshot()
            if not state.get("running"):
                return
            # snapshot() can adopt another unfinished session; signal that owner too.
            (self.directory / "stop").touch()
            worker_exited = self.process is not None and self.process.poll() is not None
            if self.process is None:
                try:
                    journal = _read(self.directory / "recovery.json")
                    worker_exited = not journal.get("worker_pid") or not process_alive(journal["worker_pid"])
                except (OSError, ValueError) as exc:
                    raise RuntimeError("无法读取上次恢复记录，请在 Windows 设置中检查网卡：" + str(exc)) from exc
            if worker_exited:
                try:
                    with AdapterSessionLock():
                        self._state = recover_session(self.directory)
                    if self.adopt_pending():
                        (self.directory / "stop").touch()
                        continue
                    return
                except Exception as exc:
                    raise RuntimeError("尚未确认网卡恢复，兜底恢复任务保留：" + str(exc)) from exc
            time.sleep(.15)
        raise TimeoutError("网卡仍在恢复，请稍后再次停止；独立恢复任务仍保留")
