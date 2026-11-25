"""Explicit-target administrator verification of real Windows adapter outages.

This is an opt-in live test: --adapter-guid is mandatory.  Each invocation keeps
its recovery journals in .localdata/adapter-live/<uuid> and writes progress to
--output.  Never use an already disabled or absent adapter for this test.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from netlab import __version__
from netlab.adapter_control import (
    WindowsAdapterControl, canonical_ids, process_alive, run_powershell,
    validated_task_name,
)
from netlab.adapter_engine import AdapterEngine, _read, _write, recover_session


def now():
    return datetime.now().astimezone().isoformat()


def config_for(guid, *, duration=5, delay=0):
    return dict(blackout=True, blackout_mode="adapter", adapter_ids=[guid],
                scope="all", protocol="all", direction="both", host="",
                ports="", advanced_filter="", start_delay_s=delay,
                blackout_duration_s=duration, blackout_loop=False)


def inspect_adapter(guid):
    """Read the actual numeric AdminStatus rather than infer it from a boolean."""
    guid = canonical_ids([guid])[0]
    script = f"$id=[Guid]'{guid}'; " + """
$adapters=@(Get-NetAdapter -IncludeHidden | Where-Object { [Guid]$_.InterfaceGuid -eq $id })
if ($adapters.Count -ne 1) { throw 'Expected exactly one target adapter' }
$a=$adapters[0]
[pscustomobject]@{guid=([Guid]$a.InterfaceGuid).ToString().ToUpperInvariant();
name=$a.Name;description=$a.InterfaceDescription;admin_status=[int]$a.AdminStatus;
status=[string]$a.Status;connected=([string]$a.Status -eq 'Up');physical=[bool]$a.HardwareInterface} |
ConvertTo-Json -Compress
"""
    row = json.loads(run_powershell(script))
    row["observed_at"] = now()
    return row


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def start_engine(engine, config, worker_exe=None):
    if worker_exe is None:
        engine.start(config)
        return
    with mock.patch.object(sys, "frozen", True, create=True), \
            mock.patch.object(sys, "executable", str(worker_exe)):
        engine.start(config)


class LiveVerification:
    def __init__(self, guid, output, mode, worker_exe=None):
        self.guid = guid
        self.worker_exe = worker_exe
        self.output = Path(output).resolve()
        self.run_root = ROOT / ".localdata" / "adapter-live" / uuid.uuid4().hex
        self.control = WindowsAdapterControl()
        self.engines = []
        self.child = None
        self.claimed_original_enabled = False
        self.report = dict(version=__version__, started_at=now(), mode=mode,
                           adapter_guid=guid, session_root=str(self.run_root),
                           passed=False, complete=False, cases=[])

    def save(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        _write(self.output, self.report)

    def record(self, case, label, **values):
        case.setdefault("steps", []).append(dict(at=now(), label=label, **values))
        self.save()

    def begin(self, name):
        case = dict(name=name, started_at=now(), passed=False, steps=[])
        self.report["cases"].append(case)
        self.save()
        return case

    def finish(self, case):
        case.update(passed=True, finished_at=now())
        self.save()

    def start(self, case, *, duration=5, delay=0):
        engine = AdapterEngine(root=self.run_root)
        self.engines.append(engine)
        try:
            start_engine(engine, config_for(self.guid, duration=duration, delay=delay), self.worker_exe)
        finally:
            self.record(case, "engine_start", session=str(engine.directory),
                        snapshot=engine.snapshot())
        return engine

    def state(self, case, label, *, engine=None):
        row = inspect_adapter(self.guid)
        values = dict(adapter=row)
        if engine is not None:
            values["snapshot"] = engine.snapshot()
        self.record(case, label, **values)
        return row

    def wait_stopped(self, engine, seconds=35):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = engine.snapshot()
            if not state.get("running"):
                require(not state.get("error"), str(state.get("error")))
                require(state.get("adapter_restored"), "Worker did not confirm recovery")
                if engine.process:
                    engine.process.wait(timeout=10)
                return state
            if state.get("phase") == "restore_failed":
                raise AssertionError(str(state.get("error")))
            time.sleep(.2)
        raise TimeoutError("Worker did not finish its scheduled outage and recovery")

    def standard(self):
        case = self.begin("Cancel during the 10-second delay")
        engine = self.start(case, duration=5, delay=10)
        require(engine.snapshot().get("phase") == "waiting", "Expected waiting phase")
        require(self.state(case, "waiting", engine=engine)["admin_status"] == 1,
                "Adapter was disabled during the delay")
        engine.stop()
        journal = _read(engine.directory / "recovery.json")
        require(not journal.get("attempted"), "Cancel unexpectedly attempted an outage")
        require(not journal.get("registration_requested"), "Cancel unexpectedly armed recovery")
        require(self.state(case, "cancelled", engine=engine)["admin_status"] == 1,
                "Adapter was not enabled after cancellation")
        self.record(case, "cancel_journal", journal=journal)
        self.finish(case)

        case = self.begin("Disable for 5 seconds and restore automatically")
        engine = self.start(case)
        require(engine.snapshot().get("phase") == "active", "Expected active outage")
        require(self.state(case, "disabled", engine=engine)["admin_status"] == 2,
                "Real AdminStatus did not change to Disabled (2)")
        self.wait_stopped(engine)
        require(self.state(case, "automatic_restore", engine=engine)["admin_status"] == 1,
                "Real AdminStatus did not return to Up (1)")
        self.finish(case)

        case = self.begin("Manual stop restores an active outage")
        engine = self.start(case, duration=30)
        require(self.state(case, "disabled", engine=engine)["admin_status"] == 2,
                "Manual-stop case never disabled the target")
        engine.stop()
        require(not engine.snapshot().get("running"), "Engine remained active after stop")
        require(self.state(case, "manual_restore", engine=engine)["admin_status"] == 1,
                "Manual stop did not restore the target")
        self.finish(case)
        self.owner_exit()

    def owner_exit(self):
        case = self.begin("Independent worker restores after its owner exits")
        marker = self.run_root / "child-owner.json"
        command = [sys.executable, "-B", str(Path(__file__).resolve()),
                   "--adapter-guid", self.guid, "--output", str(marker),
                   "--mode", "standard", "--child-owner", "--run-root", str(self.run_root)]
        if self.worker_exe is not None:
            command.extend(["--worker-exe", str(self.worker_exe)])
        self.child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        self.record(case, "child_started", child_pid=self.child.pid)
        self.child.wait(timeout=75)
        require(marker.exists(), "Child exited without a session report")
        detail = _read(marker)
        self.record(case, "owner_exited", child_report=detail, exit_code=self.child.returncode)
        require(self.child.returncode == 0 and detail.get("ready"),
                "Child did not reach its active outage: " + str(detail.get("error")))
        directory = Path(detail["directory"]).resolve()
        require(directory.parent == self.run_root.resolve(), "Child session escaped this test root")
        require(detail["snapshot"].get("phase") == "active", "Child did not observe active state")
        require(detail["adapter"].get("admin_status") == 2, "Child did not verify a real outage")
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            state = _read(directory / "status.json")
            if not state.get("running"):
                require(state.get("adapter_restored") and not state.get("error"),
                        "Orphan worker failed to restore: " + str(state.get("error")))
                self.record(case, "worker_recovered", snapshot=state)
                break
            time.sleep(.2)
        else:
            raise TimeoutError("Orphan worker did not recover after owner exit")
        require(self.state(case, "owner_exit_restore")["admin_status"] == 1,
                "Target stayed disabled after owner exit")
        self.finish(case)

    def fallback(self):
        case = self.begin("SYSTEM scheduled task restores after worker termination")
        engine = self.start(case, duration=3)
        state = engine.snapshot()
        journal = _read(engine.directory / "recovery.json")
        require(state.get("phase") == "active", "Expected active outage before worker kill")
        require(engine.process is not None and engine.process.poll() is None,
                "This session has no live worker")
        require(engine.process.pid == state.get("worker_pid") == journal.get("worker_pid"),
                "Worker identity mismatch; refusing to terminate anything")
        require(journal.get("attempted") and journal.get("armed_at"),
                "Fallback was not armed before the outage")
        # Only terminate the Popen child owned by this exact test session.
        engine.process.kill()
        engine.process.wait(timeout=10)
        self.record(case, "worker_terminated", worker_pid=engine.process.pid,
                    exit_code=engine.process.returncode, journal=journal)
        require(self.state(case, "disabled_after_worker_exit")["admin_status"] == 2,
                "Target was no longer disabled; scheduled recovery was not exercised")
        deadline = time.monotonic() + 95
        started = time.monotonic()
        while time.monotonic() < deadline:
            row = self.state(case, "awaiting_system_recovery")
            if row["admin_status"] == 1:
                self.record(case, "system_recovered", waited_seconds=time.monotonic() - started)
                break
            require(row["admin_status"] == 2, "Unexpected target AdminStatus while awaiting fallback")
            time.sleep(2)
        else:
            raise TimeoutError("SYSTEM scheduled recovery did not enable the target within 95 seconds")
        engine.stop()
        require(not engine.snapshot().get("running"), "Engine failed to reconcile its recovery journal")
        self.record(case, "journal_reconciled", journal=_read(engine.directory / "recovery.json"))
        self.finish(case)

    def connected(self):
        case = self.begin("Connected adapter: real 5-second outage and reconnection")
        before = self.state(case, "before")
        require(before["connected"], "Connected mode requires an initially connected adapter")
        engine = self.start(case)
        disabled = self.state(case, "disabled", engine=engine)
        require(disabled["admin_status"] == 2 and not disabled["connected"],
                "Target did not become physically disabled and disconnected")
        self.wait_stopped(engine)
        after = self.state(case, "enabled", engine=engine)
        require(after["admin_status"] == 1, "Target was not enabled after the outage")
        deadline = time.monotonic() + 45
        started = time.monotonic()
        while not after["connected"] and time.monotonic() < deadline:
            time.sleep(1)
            after = self.state(case, "awaiting_reconnection", engine=engine)
        require(after["connected"], "Target was enabled but did not reconnect within 45 seconds")
        self.record(case, "reconnected", waited_seconds=time.monotonic() - started, adapter=after)
        self.finish(case)

    def cleanup(self):
        """Restore the originally enabled target; never touch unrelated tasks."""
        result = dict(started_at=now(), errors=[], sessions=[])
        self.report["cleanup"] = result
        self.save()
        if not self.claimed_original_enabled:
            result.update(skipped=True, reason="Target was never claimed as originally enabled")
            self.save()
            return
        if self.child and self.child.poll() is None:
            # The exact child may still be starting; its worker observes owner exit.
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except Exception as exc:
                result["errors"].append("Child owner exit: " + str(exc))
        for engine in self.engines:
            try:
                engine.stop()
            except Exception as exc:
                result["errors"].append("Engine stop: " + str(exc))
        sessions = []
        for directory in self.run_root.iterdir():
            if not directory.is_dir() or not (directory / "recovery.json").exists():
                continue
            try:
                uuid.UUID(directory.name)
                journal = _read(directory / "recovery.json")
                require(canonical_ids(journal["adapter_ids"]) == [self.guid], "Unexpected journal target")
                require(journal["task"] == "MotuNet-Restore-" + directory.name,
                        "Unexpected journal task name")
                validated_task_name(journal["task"])
                (directory / "stop").touch()
                sessions.append((directory, journal))
            except Exception as exc:
                result["errors"].append("Session validation: " + str(exc))
        # Give surviving independent workers a chance to finish their own finally.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            alive = [journal for _, journal in sessions
                     if journal.get("worker_pid") and process_alive(journal["worker_pid"])]
            if not alive:
                break
            time.sleep(.2)
        try:
            self.control.restore([self.guid])
            row = inspect_adapter(self.guid)
            require(row["admin_status"] == 1, "Final restoration did not confirm AdminStatus=1")
            result.update(restored=True, adapter=row)
        except Exception as exc:
            result.update(restored=False)
            result["errors"].append("Final restore: " + str(exc))
        # Keep any still-running worker's fallback armed.  Otherwise reconcile only
        # this invocation's validated journals and delete their exact task names.
        if result.get("restored"):
            for directory, journal in sessions:
                try:
                    if journal.get("worker_pid") and process_alive(journal["worker_pid"]):
                        raise RuntimeError("Worker still alive; scheduled fallback retained")
                    recover_session(directory, control=self.control)
                    result["sessions"].append(dict(directory=str(directory), reconciled=True))
                except Exception as exc:
                    result["errors"].append(str(directory) + ": " + str(exc))
        result["finished_at"] = now()
        self.save()

    def run(self):
        self.run_root.mkdir(parents=True, exist_ok=False)
        self.save()
        try:
            require(os.name == "nt" and ctypes.windll.shell32.IsUserAnAdmin(),
                    "Administrator permission on Windows is required")
            self.control.preflight([self.guid])
            before = inspect_adapter(self.guid)
            require(before["physical"] and before["admin_status"] == 1,
                    "Target must be an originally enabled physical adapter")
            self.claimed_original_enabled = True
            self.report["original_adapter"] = before
            self.save()
            getattr(self, self.report["mode"])()
        except BaseException as exc:
            self.report["error"] = str(exc)
            self.report["traceback"] = traceback.format_exc()
        finally:
            try:
                self.cleanup()
            except BaseException as exc:
                self.report.setdefault("cleanup", {}).setdefault("errors", []).append(str(exc))
                # Final direct recovery is still attempted if an earlier cleanup step fails.
                if self.claimed_original_enabled:
                    try:
                        self.control.restore([self.guid])
                        self.report["cleanup"]["adapter"] = inspect_adapter(self.guid)
                        self.report["cleanup"]["restored"] = self.report["cleanup"]["adapter"]["admin_status"] == 1
                    except BaseException as restore_error:
                        self.report["cleanup"]["errors"].append(str(restore_error))
            cleanup = self.report.get("cleanup", {})
            self.report.update(complete=True, finished_at=now())
            self.report["passed"] = bool(self.report["cases"] and
                all(case["passed"] for case in self.report["cases"]) and
                not self.report.get("error") and cleanup.get("restored") and not cleanup.get("errors"))
            self.save()
        print(json.dumps(dict(passed=self.report["passed"], output=str(self.output),
                              session_root=str(self.run_root)), ensure_ascii=False))
        return 0 if self.report["passed"] else 1


def child_owner(args, guid):
    """Internal test helper: leave an active worker behind via os._exit()."""
    run_root = Path(args.run_root).resolve()
    allowed_root = (ROOT / ".localdata" / "adapter-live").resolve()
    require(run_root.parent == allowed_root and run_root.is_dir(), "Invalid child test root")
    require(uuid.UUID(run_root.name).hex == run_root.name, "Invalid child run identifier")
    marker = Path(args.output).resolve()
    require(marker == run_root / "child-owner.json", "Invalid child report destination")
    engine = AdapterEngine(root=run_root)
    try:
        require(os.name == "nt" and ctypes.windll.shell32.IsUserAnAdmin(), "Administrator required")
        WindowsAdapterControl().preflight([guid])
        start_engine(engine, config_for(guid, duration=30), args.worker_exe)
        state = engine.snapshot()
        adapter = inspect_adapter(guid)
        require(state.get("phase") == "active" and adapter["admin_status"] == 2,
                "Child did not reach a real active outage")
        _write(marker, dict(ready=True, directory=str(engine.directory),
                            snapshot=state, adapter=adapter, owner_pid=os.getpid()))
    except BaseException as exc:
        _write(marker, dict(ready=False, directory=str(engine.directory), error=str(exc)))
        engine.stop()
        return 1
    # Deliberately skip Python finally/atexit to verify the independent recovery.
    os._exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-guid", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("standard", "fallback", "connected"), default="standard")
    parser.add_argument("--worker-exe", type=Path,
                        help="Use this packaged EXE as the independent adapter worker")
    parser.add_argument("--child-owner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-root", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_exe is not None:
        args.worker_exe = args.worker_exe.resolve()
        require(args.worker_exe.is_file(), "--worker-exe must point to an existing EXE file")
    guid = canonical_ids([args.adapter_guid])[0]
    if args.child_owner:
        require(args.run_root is not None, "Child mode requires its parent test root")
        return child_owner(args, guid)
    require(args.run_root is None, "--run-root is reserved for the child owner")
    return LiveVerification(guid, args.output, args.mode, args.worker_exe).run()


if __name__ == "__main__":
    raise SystemExit(main())
