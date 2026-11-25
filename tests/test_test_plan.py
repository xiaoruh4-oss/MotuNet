import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from netlab.config import DEFAULT_CONFIG
from netlab.test_plan import (
    BusinessSceneStore, build_test_plan, builtin_business_scenes,
    import_business_scenes, normalize_business_scene,
)


class TestPlanTests(unittest.TestCase):
    def test_builtin_scenes_use_risk_based_matrix(self):
        scenes = builtin_business_scenes()
        self.assertEqual({scene["id"] for scene in scenes},
                         {"login", "lobby", "battle", "transaction"})
        for scene in scenes:
            matrix = scene["network_case_ids"]
            self.assertLessEqual(len(matrix["smoke"]), len(matrix["standard"]))
            self.assertLessEqual(len(matrix["standard"]), len(matrix["full"]))
            self.assertLess(len(matrix["full"]), 15)

    def test_build_test_plan_preserves_target_and_resets_baseline_impairments(self):
        base = dict(DEFAULT_CONFIG, scope="endpoint", host="192.0.2.10", ports="443",
                    delay_ms=900.0, loss_pct=50.0)
        plan = build_test_plan(base, builtin_business_scenes()[:1], "smoke", system_name="登录系统")
        self.assertEqual(plan["system_name"], "登录系统")
        self.assertEqual(len(plan["cases"]), 3)
        self.assertEqual(plan["cases"][0]["network_case_id"], "CONN-01")
        self.assertEqual(plan["cases"][0]["config"]["delay_ms"], 0.0)
        self.assertEqual(plan["cases"][0]["config"]["loss_pct"], 0.0)
        for case in plan["cases"]:
            self.assertEqual(case["config"]["host"], "192.0.2.10")
            self.assertEqual(case["config"]["ports"], "443")
            self.assertIn("业务操作：", case["steps"])
            self.assertIn("网络场景预期：", case["expected"])

    def test_levels_generate_ordered_scene_groups_without_full_cartesian_product(self):
        scenes = builtin_business_scenes()[:2]
        smoke = build_test_plan(DEFAULT_CONFIG, scenes, "smoke")
        standard = build_test_plan(DEFAULT_CONFIG, scenes, "standard")
        full = build_test_plan(DEFAULT_CONFIG, scenes, "full")
        self.assertLess(len(smoke["cases"]), len(standard["cases"]))
        self.assertLess(len(standard["cases"]), len(full["cases"]))
        self.assertEqual([row["business_scene_id"] for row in smoke["cases"][:3]], ["login"] * 3)
        self.assertEqual(len({row["id"] for row in full["cases"]}), len(full["cases"]))
        self.assertLess(len(full["cases"]), 30)

    def test_plan_reports_execution_budget_and_blocks_overlong_matrix_at_start_boundary(self):
        smoke = build_test_plan(DEFAULT_CONFIG, builtin_business_scenes(), "smoke")
        self.assertEqual(smoke["planned_minutes"], 41)
        self.assertEqual(smoke["time_budget_minutes"], 60)
        self.assertTrue(smoke["within_time_budget"])
        standard = build_test_plan(DEFAULT_CONFIG, builtin_business_scenes(), "standard")
        self.assertEqual(standard["planned_minutes"], 97)
        self.assertFalse(standard["within_time_budget"])
        self.assertTrue(all(case["estimated_minutes"] >= 1 for case in standard["cases"]))

    def test_custom_level_uses_explicit_conditions(self):
        scene = builtin_business_scenes()[0]
        plan = build_test_plan(DEFAULT_CONFIG, [scene], "custom", ["CONN-01", "PROTO-01"])
        self.assertEqual([row["network_case_id"] for row in plan["cases"]],
                         ["CONN-01", "PROTO-01"])
        with self.assertRaisesRegex(ValueError, "至少一个"):
            build_test_plan(DEFAULT_CONFIG, [scene], "custom")
        with self.assertRaisesRegex(ValueError, "未知网络条件"):
            build_test_plan(DEFAULT_CONFIG, [scene], "custom", ["DOES-NOT-EXIST"])

    def test_scene_validation_rejects_bad_matrix_and_duplicate_ids(self):
        scene = builtin_business_scenes()[0]
        broken = json.loads(json.dumps(scene))
        broken["network_case_ids"]["standard"] = ["CONN-01"]
        with self.assertRaisesRegex(ValueError, "全部冒烟"):
            normalize_business_scene(broken)
        with self.assertRaisesRegex(ValueError, "重复"):
            build_test_plan(DEFAULT_CONFIG, [scene, scene])

    def test_import_and_store_json_scenes(self):
        custom = builtin_business_scenes()[0]
        custom["id"] = "project-login"
        custom["name"] = "项目登录"
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "import.json"
            source.write_text(json.dumps({"schema_version": 1, "scenes": [custom]}, ensure_ascii=False),
                              encoding="utf-8")
            self.assertEqual(import_business_scenes(source)[0]["id"], "project-login")
            store = BusinessSceneStore(root / "scenes")
            self.assertEqual(store.import_file(source)[0]["name"], "项目登录")
            self.assertIn("project-login", {scene["id"] for scene in store.list()})
            with self.assertRaisesRegex(ValueError, "已存在"):
                store.import_file(source)

    def test_invalid_import_is_rejected_without_adding_partial_scenes(self):
        first, second = builtin_business_scenes()[:2]
        first["id"] = "custom-first"
        second["id"] = "login"
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "mixed.json").write_text(json.dumps({"schema_version": 1, "scenes": [first, second]},
                                                       ensure_ascii=False), encoding="utf-8")
            store = BusinessSceneStore(root)
            self.assertNotIn("custom-first", {scene["id"] for scene in store.list()})
            self.assertEqual(len(store.errors), 1)

    def test_blackout_conditions_do_not_require_loss_percent_to_drop_packets(self):
        battle = next(scene for scene in builtin_business_scenes() if scene["id"] == "battle")
        run = build_test_plan(DEFAULT_CONFIG, [battle], "smoke")
        blackout = next(row for row in run["cases"] if row["network_case_id"] == "WEAK-04")
        self.assertTrue(blackout["config"]["blackout"])
        self.assertEqual(blackout["config"]["blackout_duration_s"], 5)


if __name__ == "__main__":
    unittest.main()
