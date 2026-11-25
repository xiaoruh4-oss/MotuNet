"""Keep system adapter outages explicit and legacy packet workflows portable."""
import tempfile
import unittest

from netlab.case_suite import build_cases
from netlab.config import DEFAULT_CONFIG, PRESETS, normalize_adapter_ids, validate_config
from netlab.mobile_export import convert_desktop_scene
from netlab.scene_workflow import execution_signature, SceneWorkflow
from netlab.storage import ConfigStore
from netlab.test_plan import build_test_plan, builtin_business_scenes


FIRST = "24D2BA8A-EDA6-4F28-83B8-1AB6563215BC"
SECOND = "91FB57B0-2C30-4CC6-A418-06549ED45525"


class AdapterConfigTests(unittest.TestCase):
    def adapter_config(self, **changes):
        value = {"blackout": True, "blackout_mode": "adapter", "adapter_ids": [FIRST],
                 "blackout_duration_s": 5, "start_delay_s": 3}
        value.update(changes)
        return value

    def test_legacy_files_and_presets_keep_packet_semantics(self):
        legacy = validate_config({"blackout": True, "duration_s": 30})
        self.assertEqual(legacy["blackout_mode"], "packet")
        self.assertEqual(legacy["adapter_ids"], [])
        self.assertEqual(legacy["blackout_duration_s"], 30)
        for preset in PRESETS:
            with self.subTest(preset=preset["name"]):
                self.assertEqual(validate_config(preset["values"])["blackout_mode"], "packet")

    def test_guid_canonicalization_and_no_input_aliasing(self):
        values = ["{" + FIRST.lower() + "}", SECOND]
        config = validate_config(self.adapter_config(adapter_ids=values))
        self.assertEqual(config["adapter_ids"], [FIRST, SECOND])
        config["adapter_ids"].clear()
        self.assertEqual(len(values), 2)
        self.assertEqual(DEFAULT_CONFIG["adapter_ids"], [])

    def test_rejects_non_literal_duplicate_and_excess_adapter_guids(self):
        invalid = [None, FIRST, [1], ["WLAN"], [FIRST + ";Enable-NetAdapter"],
                   [" " + FIRST], ["{" + FIRST], [FIRST, FIRST.lower()], [FIRST] * 17]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_adapter_ids(value)

    def test_adapter_mode_requires_bounded_automatic_single_outage(self):
        for value in (0, 301, True, 1.5):
            with self.subTest(seconds=value), self.assertRaises(ValueError):
                validate_config(self.adapter_config(blackout_duration_s=value))
        with self.assertRaisesRegex(ValueError, "不支持循环"):
            validate_config(self.adapter_config(blackout_loop=True))
        for value in (1, 300):
            result = validate_config(self.adapter_config(blackout_duration_s=value, duration_s=99))
            self.assertEqual(result["duration_s"], value)
        for mode in (True, None, "shell", []):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_config(self.adapter_config(blackout_mode=mode))

    def test_adapter_mode_never_silently_broadens_packet_target(self):
        for change in ({"scope": "endpoint", "ports": "443"}, {"protocol": "tcp"},
                       {"direction": "outbound"}, {"host": "203.0.113.1"},
                       {"ports": "443"}, {"advanced_filter": "tcp"}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "不能限定"):
                validate_config(self.adapter_config(**change))

    def test_empty_selection_can_be_saved_as_draft_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root)
            value = self.adapter_config(adapter_ids=[])
            store.save(value)
            self.assertEqual(store.load()["adapter_ids"], [])
            self.assertEqual(store.load()["blackout_mode"], "adapter")

    def test_effective_signature_uses_adapter_set_and_timing(self):
        base = self.adapter_config(adapter_ids=[FIRST, SECOND])
        signature = execution_signature(base)
        other = dict(base, adapter_ids=[SECOND, FIRST], loss_pct=100, delay_ms=1200,
                     name="另一名称", duration_s=200)
        self.assertEqual(signature, execution_signature(other))
        self.assertNotEqual(signature, execution_signature(dict(base, start_delay_s=4)))
        self.assertNotEqual(signature, execution_signature(dict(base, adapter_ids=[FIRST])))
        self.assertNotEqual(signature, execution_signature(dict(base, blackout_mode="packet")))

    def test_selecting_legacy_preset_restores_packet_mode(self):
        class Draft:
            loading = busy = session_active = False
            numbers = {}

            def __init__(self, value):
                self.value = value

            def collect(self): return dict(self.value)
            def load_form(self, value): self.value = value
            def append_log(self, message): pass
            def scene_form_changed(self): pass

        draft = Draft(validate_config(self.adapter_config()))
        SceneWorkflow.select_preset(draft, 5)
        self.assertTrue(draft.value["blackout"])
        self.assertEqual(draft.value["blackout_mode"], "packet")
        self.assertEqual(draft.value["adapter_ids"], [FIRST])

    def test_mobile_export_cannot_downgrade_adapter_outage(self):
        with self.assertRaisesRegex(ValueError, "网卡断网"):
            convert_desktop_scene(self.adapter_config())
        packet = convert_desktop_scene(dict(self.adapter_config(), blackout_mode="packet"))
        self.assertTrue(packet["impairment"]["blackout"])
        self.assertNotIn("adapter_ids", packet["impairment"])
        self.assertNotIn("blackout_mode", packet["impairment"])

    def test_legacy_case_templates_never_inherit_system_adapter_mode(self):
        cases = build_cases(self.adapter_config())
        self.assertTrue(any(case["config"]["blackout"] for case in cases))
        for case in cases:
            self.assertEqual(case["config"]["blackout_mode"], "packet")
            self.assertEqual(case["config"]["adapter_ids"], [])
        run = build_test_plan(self.adapter_config(), builtin_business_scenes()[:1], "standard")
        for case in run["cases"]:
            self.assertEqual(case["config"]["blackout_mode"], "packet")
            self.assertEqual(case["config"]["adapter_ids"], [])


if __name__ == "__main__":
    unittest.main()
