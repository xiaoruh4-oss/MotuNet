import unittest
from pathlib import Path
from types import SimpleNamespace

from netlab.case_suite import build_cases, new_run
from netlab.case_workflow import CaseWorkflow
from netlab.config import DEFAULT_CONFIG


class _FakeWorkflow(CaseWorkflow):
    def __init__(self, note=""):
        run = new_run(DEFAULT_CONFIG, cases=build_cases(DEFAULT_CONFIG)[:1])
        run["cases"][0]["result"] = "进行中"
        self.case_run = run
        self.case_index = 0
        self.case_pending_result = None
        self.case_report_path = Path("test-run.json")
        self.case_list = SimpleNamespace(currentRow=lambda: 0, setCurrentRow=lambda index: None)
        self.case_actual = SimpleNamespace(toPlainText=lambda: note)
        self.engine = SimpleNamespace(snapshot=lambda: {"running": True, "received": 0, "dropped": 0})
        self.session_active = True
        self.busy = False
        self.stop_count = 0
        self.errors = []
        self.saved = []

    def error(self, message):
        self.errors.append(message)

    def render_case_flow(self):
        pass

    def stop_test(self):
        self.stop_count += 1

    def write_case_run(self):
        self.saved.append(self.case_run["overall"])

    def record(self, *args, **kwargs):
        pass

    def append_log(self, message):
        pass

    def set_running_controls(self):
        pass


class CaseWorkflowTests(unittest.TestCase):
    def test_skip_requires_reason_and_waits_for_network_restore(self):
        workflow = _FakeWorkflow()
        workflow.mark_case("跳过")
        self.assertEqual(workflow.stop_count, 0)
        self.assertIn("原因", workflow.errors[0])

        workflow.case_actual = SimpleNamespace(toPlainText=lambda: "环境暂时无法登录")
        workflow.mark_case("跳过")
        self.assertEqual(workflow.stop_count, 1)
        self.assertEqual(workflow.case_run["cases"][0]["result"], "进行中")
        workflow.case_job_done("stop", None)
        self.assertEqual(workflow.case_run["status"], "已完成")
        self.assertEqual(workflow.case_run["overall"], "未完成")
        self.assertEqual(workflow.case_run["cases"][0]["reason"], "环境暂时无法登录")

    def test_not_applicable_requires_reason_but_not_captured_packets(self):
        workflow = _FakeWorkflow("该场景没有长连接")
        workflow.mark_case("不适用")
        workflow.case_job_done("stop", None)
        self.assertEqual(workflow.case_run["overall"], "不适用")
        self.assertEqual(workflow.case_run["cases"][0]["stats"]["received"], 0)

    def test_final_report_uses_snapshot_after_network_restores(self):
        workflow = _FakeWorkflow("已确认断网后可恢复")
        workflow.mark_case("不适用")
        workflow.engine.snapshot = lambda: {"running": False, "received": 7, "dropped": 2}
        workflow.case_job_done("stop", None)
        self.assertEqual(workflow.case_run["cases"][0]["stats"]["received"], 7)
        self.assertEqual(workflow.case_run["cases"][0]["stats"]["dropped"], 2)

    def test_last_case_save_error_ends_flow_without_index_error(self):
        workflow = _FakeWorkflow("环境不适用")
        workflow.mark_case("不适用")

        def fail_save():
            raise OSError("磁盘已满")

        workflow.write_case_run = fail_save
        workflow.case_job_done("stop", None)
        self.assertEqual(workflow.case_run["status"], "保存失败")
        self.assertEqual(workflow.case_index, 0)
        self.assertIn("磁盘已满", workflow.errors[0])


if __name__ == "__main__":
    unittest.main()
