import tempfile
import unittest
from pathlib import Path

from netlab.case_suite import build_cases, new_run
from netlab.config import DEFAULT_CONFIG
from netlab.case_suite import build_cases, planned_minutes
from netlab.test_templates import TemplateStore, generic_system_template, normalize_template, standard_template


class TemplateTests(unittest.TestCase):
    def test_generic_template_has_groups_and_fifteen_cases(self):
        template = generic_system_template()
        self.assertEqual(template["id"], "generic-system-v1")
        self.assertEqual(len(template["cases"]), 15)
        self.assertGreaterEqual(len({row["group"] for row in template["cases"]}), 3)
        cases = build_cases(DEFAULT_CONFIG, template)
        self.assertEqual(len(cases), 15)
        self.assertTrue(any(row["config"]["blackout"] for row in cases))

    def test_template_normalization_rejects_duplicate_case_ids(self):
        template = standard_template()
        template["cases"].append(dict(template["cases"][0]))
        with self.assertRaises(ValueError):
            normalize_template(template)

    def test_builtin_templates_have_a_sixty_minute_execution_plan(self):
        self.assertEqual(planned_minutes(build_cases(DEFAULT_CONFIG, standard_template())), 50)
        self.assertEqual(planned_minutes(build_cases(DEFAULT_CONFIG, generic_system_template())), 60)
        self.assertTrue(all(case["estimated_minutes"] >= 1 for case in generic_system_template()["cases"]))

    def test_custom_template_can_override_case_time(self):
        template = standard_template()
        template["cases"][0]["estimated_minutes"] = 9
        normalized = normalize_template(template)
        self.assertEqual(normalized["cases"][0]["estimated_minutes"], 9)

    def test_user_template_store_round_trips_without_install_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            store = TemplateStore(Path(temp) / "test-templates")
            custom = dict(standard_template(), id="team-login-v1", name="团队登录专项")
            saved = store.save(custom)
            rows = store.list()
            self.assertTrue(any(row["id"] == "team-login-v1" for row in rows))
            self.assertEqual(saved["name"], "团队登录专项")

    def test_report_records_template_and_system_name(self):
        run = new_run(DEFAULT_CONFIG, generic_system_template(), "登录系统")
        self.assertEqual(run["template_id"], "generic-system-v1")
        self.assertEqual(run["system_name"], "登录系统")


if __name__ == "__main__":
    unittest.main()
