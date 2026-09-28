"""Behavioral guards for the managed PMM backup bucket and lifecycle rule."""
import json
import os
import subprocess
import tempfile
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

    def test_owner_marker_survives_backup_expiration_and_remains_readable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = root / "objects.json"
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "state_path = pathlib.Path(os.environ['FAKE_STORE'])\n"
                "state = json.loads(state_path.read_text()) if state_path.exists() else {'objects': {}}\n"
                "args = sys.argv[sys.argv.index('fake-mc') + 1:]\n"
                "if args[0] == 'pipe':\n"
                "    state['objects'][args[1]] = sys.stdin.read()\n"
                "elif args[0] == 'cat':\n"
                "    print(state['objects'][args[1]])\n"
                "elif args[:3] == ['ilm', 'rule', 'add']:\n"
                "    state['prefix'] = args[args.index('--prefix') + 1]\n"
                "else:\n"
                "    sys.exit(2)\n"
                "state_path.write_text(json.dumps(state))\n",
                encoding="utf-8",
            )
            docker.chmod(0o700)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", FAKE_STORE=str(store))
            context = {"pmm_backup_ws": {"path": td}, "pmm_backup_mc": "fake-mc",
                       "pmm_backup_bucket": "example", "pmm_backup_ilm_rules": [],
                       "pmm_backup": {"retention_days": 35}}

            def command(name):
                argv = TASKS[name]["ansible.builtin.command"]["argv"]
                return [str(JINJA.from_string(str(arg)).render(**context)) for arg in argv]

            marker = TASKS["Oznacz bucket PMM (nowy albo jawnie przejety)"]
            payload_template = jinja2.Environment(undefined=jinja2.StrictUndefined)
            payload_template.filters["to_json"] = json.dumps
            payload = payload_template.from_string(marker["ansible.builtin.command"]["stdin"]).render(
                platform={"name": "example"})
            written = subprocess.run(command(marker["name"]), input=payload,
                                     capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(written.returncode, 0, written.stderr)

            backup_key = "myminio/example/pmm-example-archive.tar.gz"
            state = json.loads(store.read_text(encoding="utf-8"))
            state["objects"][backup_key] = "backup"
            store.write_text(json.dumps(state), encoding="utf-8")

            rule = TASKS["Ustaw regule retencji (jedna regula, wygasanie po N dniach)"]
            shell = JINJA.from_string(rule["ansible.builtin.shell"]).render(**context)
            configured = subprocess.run(
                ["/bin/bash", "-ceu", 'PATH="$1:$PATH"\n' + shell, "--", str(root)],
                capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(configured.returncode, 0, configured.stderr)
            state = json.loads(store.read_text(encoding="utf-8"))
            marker_key = next(key for key in state["objects"] if key != backup_key)
            state["objects"] = {key: value for key, value in state["objects"].items()
                                if not key.split("/", 2)[-1].startswith(state["prefix"])}
            self.assertNotIn(backup_key, state["objects"], "backup must expire")
            self.assertIn(marker_key, state["objects"], "owner marker must not expire")
            store.write_text(json.dumps(state), encoding="utf-8")

            read_back = subprocess.run(
                command("Sprawdz zapis znacznika PMM przed zmianami ILM"),
                capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(read_back.returncode, 0, read_back.stderr)
            guard = TASKS["Wymagaj zgodnego znacznika PMM"]["ansible.builtin.assert"]["that"]
            self.assertTrue(all(evaluate(condition,
                                         pmm_backup_owner_final={"stdout": read_back.stdout},
                                         platform={"name": "example"}) for condition in guard))


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
                response = {"status": "success", "config": {"Rules": [rule]}}
                rules = JINJA.from_string(extract).render(
                    pmm_backup_ilm={"rc": 0}, pmm_backup_ilm_response=response)
                self.assertEqual(evaluate(change, pmm_backup_ilm_rules=rules,
                                          pmm_backup={"retention_days": 35}), needs_change)
        self.assertTrue(evaluate(change, pmm_backup_ilm_rules=[],
                                 pmm_backup={"retention_days": 35}))
        self.assertTrue(evaluate(change, pmm_backup_ilm_rules=[base, base],
                                 pmm_backup={"retention_days": 35}))

    def test_ilm_read_distinguishes_empty_configuration_from_failures(self):
        parse = TASKS["Rozpoznaj odpowiedz odczytu ILM"]["ansible.builtin.set_fact"]["pmm_backup_ilm_response"]
        guard = TASKS["Odmow zmiany ILM przy bledzie odczytu"]["ansible.builtin.assert"]["that"]
        extract = TASKS["Wylicz obecne reguly retencji"]["ansible.builtin.set_fact"]["pmm_backup_ilm_rules"]
        change = TASKS["Ustaw regule retencji (jedna regula, wygasanie po N dniach)"]["when"]
        rule = {"Status": "Enabled", "Filter": {"Prefix": "pmm-"},
                "Expiration": {"Days": 35}}
        absent = {"status": "error", "error": {
            "message": "Unable to get lifecycle",
            "cause": {"message": "The lifecycle configuration does not exist"}}}
        empty = {"status": "error", "error": {
            "message": "Unable to ls lifecycle configuration",
            "cause": {"message": "lifecycle configuration not set"}}}
        cases = [
            ("absent", 1, absent, True, True),
            ("empty", 1, empty, True, True),
            ("denied", 1, {"status": "error", "error": {
                "message": "Unable to get lifecycle", "cause": {"message": "Access Denied"}}}, False, False),
            ("transient", 1, {"status": "error", "error": {
                "message": "Unable to ls lifecycle configuration", "cause": {"message": "timed out"}}}, False, False),
            ("inconsistent", 0, absent, False, False),
            ("converged", 0, {"status": "success", "config": {"Rules": [rule]}}, True, False),
            ("drifted", 0, {"status": "success", "config": {"Rules": []}}, True, True),
        ]
        for label, rc, payload, permitted, mutate in cases:
            with self.subTest(label=label):
                read = {"rc": rc, "stdout": json.dumps(payload)}
                response = JINJA.from_string(parse).render(pmm_backup_ilm=read)
                allowed = all(evaluate(condition, pmm_backup_ilm=read,
                                       pmm_backup_ilm_response=response) for condition in guard)
                self.assertEqual(allowed, permitted)
                if allowed:
                    rules = JINJA.from_string(extract).render(
                        pmm_backup_ilm=read, pmm_backup_ilm_response=response)
                    self.assertEqual(evaluate(change, pmm_backup_ilm_rules=rules,
                                              pmm_backup={"retention_days": 35}), mutate)
        with self.assertRaises(ValueError):
            JINJA.from_string(parse).render(pmm_backup_ilm={"rc": 1, "stdout": "not JSON"})


if __name__ == "__main__":
    unittest.main()
