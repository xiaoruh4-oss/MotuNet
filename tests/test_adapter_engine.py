"""Recovery ordering is tested with fake adapters, never the machine's network."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
from contextlib import nullcontext

from netlab.adapter_engine import (AdapterEngine, _read, _write, pending_recoveries,
                                   recover_session, run_session, validate_adapter_config)
from netlab.adapter_engine import worker_main
from netlab.adapter_control import canonical_ids, WindowsAdapterControl, run_powershell, _powershell_error, list_adapters

GUID = "24D2BA8A-EDA6-4F28-83B8-1AB6563215BC"


class FakeTime:
    def __init__(self): self.now = 100.0
    def clock(self): return self.now
    def sleep(self, duration): self.now += duration


class Parent:
    def __init__(self, alive=lambda: True): self.condition = alive
    def alive(self): return self.condition()
    def close(self): pass


class Control:
    def __init__(self, directory):
        self.directory = directory
        self.calls = []
        self.disabled = False
        self.fail_register = False
        self.fail_disable = False
        self.fail_restore = False
        self.fail_cleanup = False
        self.pending = []
    def preflight(self, ids):
        self.calls.append("preflight")
        return [dict(guid=GUID, name="测试物理网卡", admin_up=True)]
    def pending_tasks(self): return self.pending
    def register_recovery(self, task, ids, seconds):
        self.calls.append("register")
        assert (self.directory / "recovery.json").exists()
        assert seconds == 46
        if self.fail_register: raise OSError("task registration denied")
    def disable(self, ids):
        self.calls.append("disable")
        assert "register" in self.calls
        assert _read(self.directory/"recovery.json")["attempted"]
        assert _read(self.directory/"status.json")["phase"] == "disabling"
        self.disabled = True
        if self.fail_disable: raise OSError("partial disable")
    def restore(self, ids):
        self.calls.append("restore")
        if self.fail_restore: raise OSError("device unavailable")
        self.disabled = False
    def remove_recovery(self, task):
        self.calls.append("remove")
        assert not self.disabled
        if self.fail_cleanup: raise OSError("task cleanup failed")


class AdapterSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)/uuid.uuid4().hex
        self.directory.mkdir()
        self.config = dict(blackout=True, blackout_mode="adapter", adapter_ids=[GUID],
                           start_delay_s=1, blackout_duration_s=1, blackout_loop=False,
                           scope="all", protocol="all", direction="both")
        _write(self.directory/"request.json", dict(config=self.config, parent_pid=123))
        self.control = Control(self.directory)
        self.time = FakeTime()
    def run_session(self, parent=None):
        return run_session(self.directory, self.control, parent or Parent(),
                           clock=self.time.clock, sleep=self.time.sleep)
    def test_atomic_status_write_retries_a_transient_windows_read_collision(self):
        target=self.directory/'status.json'
        _write(target,dict(phase='starting'))
        real_replace=os.replace
        calls=[]
        def replace(source,destination):
            calls.append((source,destination))
            if len(calls)==1:
                raise PermissionError('reader temporarily prevents atomic replace')
            return real_replace(source,destination)
        with patch('netlab.adapter_engine.os.replace',side_effect=replace), \
             patch('netlab.adapter_engine.time.sleep') as sleep:
            _write(target,dict(phase='waiting'))
        self.assertEqual(len(calls),2)
        sleep.assert_called_once_with(.025)
        self.assertEqual(_read(target),dict(phase='waiting'))
        self.assertEqual(list(self.directory.glob('status.json.tmp-*')),[])
    def test_atomic_write_does_not_retry_permission_errors_indefinitely(self):
        target=self.directory/'status.json'
        with patch('netlab.adapter_engine.os.replace',side_effect=PermissionError('access denied')) as replace, \
             patch('netlab.adapter_engine.time.sleep') as sleep:
            with self.assertRaises(PermissionError):
                _write(target,dict(phase='waiting'))
        self.assertEqual(replace.call_count,21)
        self.assertEqual(sleep.call_count,20)
        self.assertEqual(list(self.directory.glob('status.json.tmp-*')),[])
    def test_timed_outage_arms_before_mutation_and_restores_before_removing_task(self):
        state = self.run_session()
        self.assertEqual(self.control.calls, ["preflight", "preflight", "register", "disable", "restore", "remove"])
        self.assertEqual(state["phase"], "stopped")
        self.assertFalse(state["running"])
        self.assertTrue(state["adapter_restored"])
        self.assertFalse(self.control.disabled)
        self.assertTrue(_read(self.directory/"recovery.json")["restored"])
    def test_registration_failure_never_disables(self):
        self.control.fail_register = True
        state = self.run_session()
        self.assertNotIn("disable", self.control.calls)
        self.assertFalse(state["running"])
        self.assertEqual(state["phase"], "error")
        self.assertIn("registration", state["error"])
        self.assertIn("remove", self.control.calls)
    def test_parent_exit_during_delay_does_not_touch_adapter(self):
        state = self.run_session(Parent(lambda:self.time.now<100.5))
        self.assertEqual(self.control.calls, ["preflight"])
        self.assertFalse(state["running"])
        self.assertTrue(state["adapter_restored"])
    def test_parent_exit_while_active_restores_without_waiting_full_duration(self):
        state = self.run_session(Parent(lambda:self.time.now<101.2))
        self.assertIn("restore", self.control.calls)
        self.assertTrue(state["adapter_restored"])
        self.assertLess(state["active_elapsed"], 1)
    def test_partial_disable_failure_still_restores_all_claimed_adapters(self):
        self.control.fail_disable = True
        state = self.run_session()
        self.assertIn("restore", self.control.calls)
        self.assertIn("remove", self.control.calls)
        self.assertTrue(state["adapter_restored"])
        self.assertFalse(state["running"])
    def test_failed_restore_stays_running_and_keeps_fallback(self):
        self.control.fail_restore = True
        state = self.run_session()
        self.assertEqual(self.control.calls.count("restore"),3)
        self.assertNotIn("remove",self.control.calls)
        self.assertTrue(state["running"])
        self.assertEqual(state["phase"],"restore_failed")
        self.assertTrue(state["recovery_pending"])
        self.assertFalse(_read(self.directory/"recovery.json")["restored"])
    def test_cleanup_failure_does_not_claim_network_is_still_disabled(self):
        self.control.fail_cleanup = True
        state = self.run_session()
        self.assertFalse(state["running"])
        self.assertTrue(state["adapter_restored"])
        self.assertIn("cleanup",state["recovery_cleanup_warning"])
    def test_unresolved_previous_task_blocks_new_outage(self):
        self.control.pending=["MotuNet-Restore-existing"]
        state=self.run_session()
        self.assertNotIn("disable",self.control.calls)
        self.assertEqual(state["phase"],"error")
    def test_dead_worker_journal_can_be_recovered(self):
        self.control.fail_restore=True
        self.run_session()
        self.control.fail_restore=False
        with patch("netlab.adapter_engine.process_alive",return_value=False):
            state=recover_session(self.directory,control=self.control)
        self.assertFalse(state["running"])
        self.assertTrue(state["adapter_restored"])
    def test_live_worker_is_not_raced_by_startup_recovery(self):
        self.control.fail_restore=True
        self.run_session()
        with patch("netlab.adapter_engine.process_alive",return_value=True):
            with self.assertRaisesRegex(RuntimeError,"仍在运行"):
                recover_session(self.directory,control=self.control)
    def test_recovery_rejects_devices_not_originally_enabled(self):
        self.run_session()
        journal=_read(self.directory/"recovery.json")
        journal["originally_enabled"]=[]
        _write(self.directory/"recovery.json",journal)
        with self.assertRaises(ValueError):
            recover_session(self.directory,control=self.control)
    def test_completed_history_is_not_adopted(self):
        self.run_session()
        self.assertEqual(pending_recoveries(self.directory.parent), [])
        self.assertFalse(AdapterEngine(root=self.directory.parent).adopt_pending())
    def test_pending_session_is_adopted_without_spawning_a_worker(self):
        self.control.fail_restore=True
        self.run_session()
        engine=AdapterEngine(root=self.directory.parent)
        self.assertTrue(engine.adopt_pending())
        self.assertIsNone(engine.process)
        self.assertTrue(engine.snapshot()["running"])
        self.assertEqual(engine.snapshot()["phase"],"restore_failed")
        self.assertEqual(engine.directory,self.directory)
    def test_exited_worker_cannot_hide_pending_journal_with_stopped_status(self):
        self.control.fail_restore=True
        self.run_session()
        _write(self.directory/"status.json",dict(running=False,phase="stopped"))
        engine=AdapterEngine(root=self.directory.parent)
        engine.directory=self.directory
        engine.process=Mock()
        engine.process.poll.return_value=1
        self.assertTrue(engine.snapshot()["running"])
        self.assertTrue(engine.snapshot()["recovery_pending"])
    def test_outer_worker_failure_keeps_persisted_recovery_responsibility(self):
        self.control.fail_restore=True
        self.run_session()
        with patch('netlab.adapter_engine.ProcessWatch',return_value=Parent()), \
             patch('netlab.adapter_engine.AdapterSessionLock',return_value=nullcontext()), \
             patch('netlab.adapter_engine.run_session',side_effect=OSError('unexpected worker crash')):
            self.assertEqual(worker_main(self.directory),1)
        state=_read(self.directory/'status.json')
        self.assertTrue(state['running'])
        self.assertEqual(state['phase'],'restore_failed')
    def test_early_worker_exit_without_journal_finishes_as_startup_error(self):
        _write(self.directory/'status.json',dict(running=True,phase='starting'))
        engine=AdapterEngine(root=self.directory.parent)
        engine.directory=self.directory
        engine.process=Mock()
        engine.process.poll.return_value=1
        state=engine.snapshot()
        self.assertFalse(state['running'])
        self.assertFalse(state['recovery_pending'])
        self.assertFalse(state['adapter_attempted'])
        self.assertEqual(state['phase'],'error')
        self.assertIn('尚未执行禁用网卡',state['error'])
        with patch('netlab.adapter_engine.recover_session') as recover:
            engine.stop()
        recover.assert_not_called()
    def test_exited_worker_with_corrupt_journal_keeps_recovery_responsibility(self):
        _write(self.directory/'status.json',dict(running=True,phase='starting'))
        (self.directory/'recovery.json').write_text('{corrupt',encoding='utf-8')
        engine=AdapterEngine(root=self.directory.parent)
        engine.directory=self.directory
        engine.process=Mock()
        engine.process.poll.return_value=1
        state=engine.snapshot()
        self.assertTrue(state['running'])
        self.assertTrue(state['recovery_pending'])
        self.assertEqual(state['phase'],'restore_failed')
    def test_stop_drains_multiple_abandoned_sessions(self):
        self.control.fail_restore=True
        self.run_session()
        second=self.directory.parent/uuid.uuid4().hex
        second.mkdir()
        journal=_read(self.directory/'recovery.json')
        journal['task']='MotuNet-Restore-'+second.name
        _write(second/'recovery.json',journal)
        self.control.fail_restore=False
        engine=AdapterEngine(root=self.directory.parent)
        self.assertTrue(engine.adopt_pending())
        recover=lambda directory:recover_session(directory,control=self.control)
        with patch('netlab.adapter_engine.AdapterSessionLock',return_value=nullcontext()), \
             patch('netlab.adapter_engine.process_alive',return_value=False), \
             patch('netlab.adapter_engine.recover_session',side_effect=recover):
            engine.stop()
        self.assertEqual(pending_recoveries(self.directory.parent),[])
        self.assertFalse(engine.snapshot()['running'])
        self.assertTrue((second/'stop').exists())


class AdapterValidationTests(unittest.TestCase):
    def test_hidden_physical_adapter_cannot_bypass_selection_with_old_guid(self):
        control=WindowsAdapterControl()
        hidden=dict(guid=GUID,name='WLAN 2',physical=True,hidden=True,admin_up=True,status='Disconnected')
        with patch.object(control,'inspect',return_value=[hidden]):
            with self.assertRaisesRegex(ValueError,'隐藏接口'):
                control.preflight([GUID])
    def test_clixml_error_keeps_first_message_without_progress_or_xml(self):
        stderr=('#< CLIXML\r\n<Objs xmlns="http://schemas.microsoft.com/powershell/2004/04">'
                '<S S="Warning">ignore me</S><S S="Error">Disable-NetAdapter : 不支持此请求。_x000D__x000A_</S>'
                '<S S="Error">stack trace_x000D__x000A_</S></Objs>').encode('utf-8')
        self.assertEqual(_powershell_error(stderr),'Disable-NetAdapter : 不支持此请求。')
        self.assertEqual(_powershell_error(b'#< CLIXML\n<broken'),'#< CLIXML\n<broken')
        self.assertEqual(_powershell_error(b'plain error'),'plain error')
    @unittest.skipUnless(os.name=='nt','PowerShell-specific command behavior')
    def test_hidden_adapter_is_rejected_again_immediately_before_disable(self):
        with patch('netlab.adapter_control.run_powershell',return_value='') as run:
            WindowsAdapterControl().disable([GUID])
        script="function Get-NetAdapter { param([switch]$IncludeHidden); " \
               "[pscustomobject]@{InterfaceGuid='"+GUID+"';HardwareInterface=$true;Hidden=$true;AdminStatus=1;Status='Disconnected'} }\n" \
               "function Disable-NetAdapter { throw 'must not disable anything' }\n" \
               +run.call_args.args[0]
        with self.assertRaisesRegex(OSError,'enabled visible physical adapter'):
            run_powershell(script)
    @unittest.skipUnless(os.name=='nt','PowerShell-specific command behavior')
    def test_selection_omits_hidden_and_absent_physical_interfaces(self):
        with patch('netlab.adapter_control.run_powershell',return_value='') as run:
            list_adapters()
        script="function Get-NetAdapter { " \
               "[pscustomobject]@{InterfaceGuid='"+GUID+"';Name='WLAN';HardwareInterface=$true;Hidden=$false;AdminStatus=1;Status='Up'}; " \
               "[pscustomobject]@{InterfaceGuid='"+GUID+"';Name='WLAN 2';HardwareInterface=$true;Hidden=$true;AdminStatus=1;Status='Disconnected'}; " \
               "[pscustomobject]@{InterfaceGuid='"+GUID+"';Name='Absent';HardwareInterface=$true;Hidden=$false;AdminStatus=1;Status='Not Present'} }\n" \
               +run.call_args.args[0]
        rows=json.loads(run_powershell(script))
        self.assertEqual(rows['name'],'WLAN')
        self.assertFalse(rows['hidden'])
    @unittest.skipUnless(os.name=='nt','PowerShell-specific command behavior')
    def test_removing_an_absent_recovery_task_is_successful(self):
        with patch('netlab.adapter_control.run_powershell',return_value='') as run:
            WindowsAdapterControl().remove_recovery('MotuNet-Restore-'+uuid.uuid4().hex)
        # Shadow cmdlets with local functions: this never changes Task Scheduler.
        script="function Get-ScheduledTask { [CmdletBinding()] param(); @() }\n" \
               "function Unregister-ScheduledTask { throw 'must not remove anything' }\n" \
               +run.call_args.args[0]
        self.assertEqual(run_powershell(script),'')
    @unittest.skipUnless(os.name=='nt','PowerShell-specific command behavior')
    def test_real_task_cleanup_failures_are_not_silenced(self):
        task='MotuNet-Restore-'+uuid.uuid4().hex
        with patch('netlab.adapter_control.run_powershell',return_value='') as run:
            WindowsAdapterControl().remove_recovery(task)
        script="function Get-ScheduledTask { [CmdletBinding()] param(); " \
               "[pscustomobject]@{TaskName='"+task+"';TaskPath='\\'} }\n" \
               "function Unregister-ScheduledTask { [CmdletBinding(SupportsShouldProcess)] param([Parameter(ValueFromPipeline)] $InputObject); process { throw 'simulated cleanup denied' } }\n" \
               +run.call_args.args[0]
        with self.assertRaisesRegex(OSError,'simulated cleanup denied'):
            run_powershell(script)
    def test_rejects_unsafe_configs(self):
        base=dict(blackout=True,blackout_mode="adapter",adapter_ids=[GUID],blackout_duration_s=10)
        for change in (dict(adapter_ids=[]),dict(adapter_ids=["name'; Disable-NetAdapter *"]),
                       dict(adapter_ids=[GUID,GUID]),dict(blackout_duration_s=0),
                       dict(blackout_duration_s=301),dict(blackout_duration_s=True),
                       dict(blackout_loop=True),dict(scope="endpoint"),dict(direction="inbound"),
                       dict(ports="443"),dict(start_delay_s=-1)):
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_adapter_config(dict(base,**change))
    def test_guid_normalization_and_command_targets(self):
        self.assertEqual(canonical_ids(["{"+GUID.lower()+"}"]),[GUID])
        with patch("netlab.adapter_control.run_powershell",return_value="") as run:
            WindowsAdapterControl().disable([GUID])
            script=run.call_args.args[0]
        self.assertIn("[Guid]'"+GUID+"'",script)
        self.assertIn("HardwareInterface",script)
        self.assertIn("AdminStatus -ne 1",script)
        self.assertNotIn("-Name *",script)
    def test_recovery_action_is_embedded_not_a_user_writable_script_path(self):
        with patch("netlab.adapter_control.run_powershell",return_value="") as run:
            WindowsAdapterControl().register_recovery("MotuNet-Restore-"+uuid.uuid4().hex,[GUID],55)
        script=run.call_args.args[0]
        self.assertIn("-UserId 'SYSTEM'",script)
        self.assertIn("-EncodedCommand",script)
        self.assertIn("StartWhenAvailable",script)
        self.assertNotIn("-File",script)


if __name__ == "__main__": unittest.main()
