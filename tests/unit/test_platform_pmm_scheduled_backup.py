"""Behavioral guards for the managed PMM backup bucket and lifecycle rule."""
import json
import unittest
from pathlib import Path

import jinja2
from jinja2.nativetypes import NativeEnvironment
import yaml


REPO = Path(__file__).resolve().parents[2]
PLAY = yaml.safe_load((REPO / "playbooks/platform_pmm_backup.yml").read_text())[0]
TASKS = {task["name"]: task for task in PLAY["tasks"][1]["block"]}
JINJA = NativeEnvironment(undefined=jinja2.StrictUndefined)
JINJA.filters["from_json"] = json.loads
JINJA.filters["bool"] = bool


def evaluate(expression, **context):
    return JINJA.from_string("{{ " + expression + " }}").render(**context)


class TestPmmBackupBucketOwnership(unittest.TestCase):
    def test_existing_unmarked_bucket_requires_explicit_adoption(self):
        guard = TASKS["Odmow pracy na istniejacym buckecie bez znacznika"]
        condition = guard["ansible.builtin.assert"]["that"][0]
        self.assertFalse(evaluate(condition, pmm_backup_bucket_stat={"rc": 0}))
        self.assertTrue(evaluate(condition, pmm_backup_bucket_stat={"rc": 1}))
        self.assertTrue(evaluate(condition, pmm_backup_bucket_stat={"rc": 0},
                                 pmm_backup_adopt_existing_bucket=True))
        self.assertEqual(guard["when"], "pmm_backup_owner_initial.rc != 0")

    def test_foreign_or_invalid_owner_never_passes_validation(self):
        conditions = TASKS["Potwierdz wlasciciela istniejacego bucketa PMM"]["ansible.builtin.assert"]["that"]
        for owner, expected in [
            ({"format_version": 1, "platform_name": "xenonv17"}, True),
            ({"format_version": 1, "platform_name": "foreign"}, False),
            ({"format_version": 2, "platform_name": "xenonv17"}, False),
        ]:
            with self.subTest(owner=owner):
                context = {"pmm_backup_owner_initial": {"stdout": json.dumps(owner)},
                           "platform": {"name": "xenonv17"}}
                self.assertEqual(all(evaluate(condition, **context) for condition in conditions), expected)

    def test_marker_survives_expiration_and_all_steps_use_one_key(self):
        marker = "isa-pmm-backup-owner.json"
        self.assertFalse(marker.startswith("pmm-"))
        for name in ["Odczytaj znacznik wlasciciela bucketa PMM",
                     "Oznacz bucket PMM (nowy albo jawnie przejety)",
                     "Sprawdz zapis znacznika PMM przed zmianami ILM"]:
            argv = TASKS[name]["ansible.builtin.command"]["argv"]
            self.assertEqual(argv[-1], "myminio/{{ pmm_backup_bucket }}/" + marker)


class TestPmmBackupLifecycle(unittest.TestCase):
    def test_only_exact_enabled_prefix_and_days_converges(self):
        extract = TASKS["Wylicz obecne reguly retencji"]["ansible.builtin.set_fact"]["pmm_backup_ilm_rules"]
        change = TASKS["Ustaw regule retencji (jedna regula, wygasanie po N dniach)"]["when"]
        base = {"ID": "random", "Status": "Enabled", "Filter": {"Prefix": "pmm-"},
                "Expiration": {"Days": 35}}
        cases = [(base, False),
                 ({**base, "Status": "Disabled"}, True),
                 ({**base, "Filter": {"Prefix": "other-"}}, True),
                 ({**base, "Expiration": {"Days": 7}}, True)]
        for rule, needs_change in cases:
            with self.subTest(rule=rule):
                response = {"config": {"Rules": [rule]}}
                rules = JINJA.from_string(extract).render(
                    pmm_backup_ilm={"stdout": json.dumps(response)})
                self.assertEqual(evaluate(change, pmm_backup_ilm_rules=rules,
                                          pmm_backup={"retention_days": 35}), needs_change)
        self.assertTrue(evaluate(change, pmm_backup_ilm_rules=[],
                                 pmm_backup={"retention_days": 35}))
        self.assertTrue(evaluate(change, pmm_backup_ilm_rules=[base, base],
                                 pmm_backup={"retention_days": 35}))


if __name__ == "__main__":
    unittest.main()
