import unittest

from netlab.case_suite import build_cases, markdown_report, new_run, overall_result
from netlab.config import DEFAULT_CONFIG
from netlab.test_plan import build_test_plan, builtin_business_scenes


class CaseSuiteTests(unittest.TestCase):
    def test_build_cases_preserves_target_and_covers_initial_matrix(self):
        base = dict(DEFAULT_CONFIG, scope="endpoint", host="192.0.2.10", ports="443")
        cases = build_cases(base)
        self.assertEqual(len(cases), 9)
        self.assertEqual(cases[0]["config"]["host"], "192.0.2.10")
        self.assertEqual(cases[0]["config"]["ports"], "443")
        self.assertTrue(cases[6]["config"]["blackout"])
        self.assertGreaterEqual(cases[8]["config"]["blackout_duration_s"], 30)
        self.assertEqual(cases[0]["result"], "待测")

    def test_overall_result_requires_all_cases_to_pass(self):
        cases = build_cases(DEFAULT_CONFIG)
        self.assertEqual(overall_result(cases), "未完成")
        for case in cases:
            case["result"] = "通过"
        self.assertEqual(overall_result(cases), "通过")
        cases[3]["result"] = "不通过"
        self.assertEqual(overall_result(cases), "不通过")

    def test_overall_result_distinguishes_not_applicable_and_skipped(self):
        cases = build_cases(DEFAULT_CONFIG)[:2]
        cases[0]["result"] = "通过"
        cases[1]["result"] = "不适用"
        self.assertEqual(overall_result(cases), "通过")
        cases[0]["result"] = "不适用"
        self.assertEqual(overall_result(cases), "不适用")
        cases[1]["result"] = "跳过"
        self.assertEqual(overall_result(cases), "未完成")
        cases[0]["result"] = "不通过"
        self.assertEqual(overall_result(cases), "不通过")

    def test_new_run_accepts_plan_rows_without_mutating_preview(self):
        plan = build_test_plan(DEFAULT_CONFIG, builtin_business_scenes()[:1], "smoke", system_name="登录系统")
        plan["cases"][0]["result"] = "通过"
        run = new_run(DEFAULT_CONFIG, system_name="登录系统", cases=plan["cases"], level="冒烟")
        self.assertEqual(run["cases"][0]["result"], "待测")
        self.assertEqual(plan["cases"][0]["result"], "通过")
        self.assertEqual(run["cases"][0]["business_scene_name"], "登录与进入系统")
        self.assertEqual(run["test_level"], "冒烟")

    def test_report_groups_business_scenes_and_shows_reasons(self):
        run = build_test_plan(DEFAULT_CONFIG, builtin_business_scenes()[:2], "smoke", system_name="游戏客户端")
        first, second = run["cases"][0], run["cases"][-1]
        first.update(result="通过", actual="登录成功", stats={"received": 12})
        second.update(result="不适用", actual="此页面没有该操作", reason="此页面没有该操作")
        report = markdown_report(run)
        self.assertIn("## 登录与进入系统", report)
        self.assertIn("## 大厅与页面加载", report)
        self.assertIn("不适用 1", report)
        self.assertIn("原因：此页面没有该操作", report)
        self.assertIn("测试级别：冒烟", report)

    def test_markdown_report_contains_verdict_and_actual_observation(self):
        run = new_run(DEFAULT_CONFIG)
        run["cases"][0]["result"] = "通过"
        run["cases"][0]["actual"] = "登录成功，恢复正常。"
        report = markdown_report(run)
        self.assertIn("总体结论：**未完成**", report)
        self.assertIn("登录成功，恢复正常。", report)
        self.assertIn("WN-01", report)
        self.assertIn("预计时长：50 / 60 分钟", report)
        self.assertIn("预计用时：3 分钟", report)
