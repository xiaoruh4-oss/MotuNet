import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from netlab.config import DEFAULT_CONFIG, PRESETS
from netlab.storage import ConfigStore


class ScenarioStoreTests(unittest.TestCase):
    def scenario(self, name):
        value = dict(DEFAULT_CONFIG)
        value["name"] = name
        return value

    def test_save_list_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            first = ConfigStore(root)
            saved = first.save_scenario(self.scenario("我的延迟场景"))
            self.assertRegex(saved["id"], r"^[0-9a-f]{32}$")
            self.assertEqual(saved["id"], Path(root, "scenarios", saved["id"] + ".json").stem)
            second = ConfigStore(root)
            self.assertEqual(second.list_scenarios(), [saved])

    def test_scenarios_are_independent_and_overwrite_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            a = store.save_scenario(self.scenario("场景A"))
            b = store.save_scenario(self.scenario("场景B"))
            self.assertEqual({x["id"] for x in store.list_scenarios()}, {a["id"], b["id"]})
            updated = store.save_scenario(self.scenario("场景A更新"), a["id"])
            self.assertEqual(updated["id"], a["id"])
            self.assertEqual({x["config"]["name"] for x in store.list_scenarios()}, {"场景A更新", "场景B"})
            updated["config"]["loss_pct"] = 99
            self.assertNotEqual(store.list_scenarios()[0]["config"]["loss_pct"], 99)
            with self.assertRaisesRegex(ValueError, "不存在"):
                store.save_scenario(self.scenario("未知ID"), "f" * 32)

    def test_name_validation_and_duplicate_rules(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            with self.assertRaisesRegex(ValueError, "场景名称不能为空"):
                store.save_scenario(self.scenario("   "))
            with self.assertRaisesRegex(ValueError, "不能超过"):
                store.save_scenario(self.scenario("x" * 81))
            with self.assertRaisesRegex(ValueError, "内置场景"):
                store.save_scenario(self.scenario("轻度弱网"))
            store.save_scenario(self.scenario("My Scene"))
            with self.assertRaisesRegex(ValueError, "已存在"):
                store.save_scenario(self.scenario(" my scene "))

    def test_corrupt_file_isolated_and_reported(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            good = store.save_scenario(self.scenario("保留场景"))
            bad = Path(root, "scenarios", "a" * 32 + ".json")
            bad.write_text("{broken", encoding="utf-8")
            Path(root, "scenarios", "b" * 32 + ".json").write_bytes(b"\xff\xfe")
            self.assertEqual(store.list_scenarios(), [good])
            self.assertEqual(len(store.scenario_errors), 2)

    def test_path_safety_and_envelope_id(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            for value in ("../escape", "A" * 32, "0" * 31, "0" * 33, ""):
                with self.assertRaises(ValueError):
                    store.save_scenario(self.scenario("safe"), value)
            saved = store.save_scenario(self.scenario("safe"))
            envelope = json.loads(Path(root, "scenarios", saved["id"] + ".json").read_text(encoding="utf-8"))
            self.assertEqual(envelope["id"], saved["id"])
            self.assertEqual(envelope["schema_version"], 1)
            envelope["id"] = "c" * 32
            Path(root, "scenarios", saved["id"] + ".json").write_text(json.dumps(envelope), encoding="utf-8")
            self.assertEqual(store.list_scenarios(), [])
            self.assertIn("ID与文件名不一致", store.scenario_errors[0])

    def test_name_never_becomes_a_file_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            saved = store.save_scenario(self.scenario("../../用户场景"))
            self.assertEqual([p.name for p in Path(root, "scenarios").iterdir()], [saved["id"] + ".json"])

    def test_atomic_write_failure_preserves_existing_scenario(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            saved = store.save_scenario(self.scenario("原来的场景"))
            with patch("netlab.storage.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(ValueError, "保存自定义场景失败"):
                    store.save_scenario(self.scenario("更新失败场景"), saved["id"])
            self.assertEqual(store.list_scenarios(), [saved])
            self.assertEqual(len(list(Path(root, "scenarios").iterdir())), 1)

    def test_builtin_presets_are_not_user_scenarios(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            original_presets = deepcopy(PRESETS)
            self.assertEqual(store.list_scenarios(), [])
            custom = store.save_scenario(self.scenario("自定义模板"))
            self.assertEqual(PRESETS, original_presets)
            saved_config = store.save(DEFAULT_CONFIG)
            self.assertEqual(store.load(), saved_config)
            self.assertEqual(store.list_scenarios(), [custom])


if __name__ == "__main__":
    unittest.main()
