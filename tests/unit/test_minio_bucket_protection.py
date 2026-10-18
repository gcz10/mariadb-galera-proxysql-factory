#!/usr/bin/env python3
"""minio_bucket_protection.yml: ochrona kopii przed nadpisaniem (P4).

Klucz zapisu lezy na kazdym wezle Galery i bez wersjonowania nadpisywal kopie.
Ochrona to Object Lock GOVERNANCE na `retention_days` plus regula ILM, ktora
zwalnia miejsce po `prune`. Blokady nie da sie dolozyc istniejacemu bucketowi.

Test wykonuje PRAWDZIWY `ansible-playbook` na `connection: local` z atrapa
`docker`, ktora gra role przypietego `mc` na stanie w pliku JSON. Ksztalty
odpowiedzi skopiowano z przypietego mc RELEASE.2025-08-13T08-35-41Z na zywym
MinIO (2026-10-18). Werdykt opiera sie na kodzie wyjscia i dzienniku mutacji
atrapy, nie na tresci pliku zadan.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TASKS = REPO / "roles" / "galera_backup" / "tasks" / "minio_bucket_protection.yml"
ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")
MC_IMAGE = "mc@sha256:test"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["FAKE_MC_STATE"]
state = json.load(open(state_path))
argv = sys.argv[1:]
mc = argv[argv.index(os.environ["FAKE_MC_IMAGE"]) + 1:]
with open(state_path + ".calls", "a") as log:
    log.write(json.dumps(mc) + "\n")

def save():
    json.dump(state, open(state_path, "w"))

def mutation(entry):
    with open(state_path + ".mutations", "a") as log:
        log.write(json.dumps(entry) + "\n")

def fail(payload):
    print(json.dumps(payload))
    sys.exit(1)

if mc[:2] == ["retention", "info"]:
    if state.get("retention_error"):
        fail({"status": "error", "error": {"message": "Access Denied.", "cause": {"message": ""}}})
    if not state["locked"]:
        fail({"status": "error", "error": {"message": "Remote bucket `%s` does not support locking",
              "cause": {"message": "", "error": {}}, "type": "fatal"}})
    print(json.dumps({"op": "info", "enabled": "Enabled", "mode": state.get("mode", ""),
                      "validity": state.get("validity", "0"), "status": "success"}))
elif mc[:2] == ["retention", "set"]:
    mode, validity = mc[3], mc[4]
    state["mode"], state["validity"] = mode.upper(), validity[:-1] + "DAYS"
    mutation(["retention set", mode, validity])
    save()
elif mc[:3] == ["ilm", "rule", "ls"]:
    if not state["rules"]:
        fail({"status": "error", "error": {"message": "Unable to get lifecycle",
              "cause": {"message": "The lifecycle configuration does not exist"}}})
    print(json.dumps({"status": "success", "config": {"Rules": state["rules"]}}))
elif mc[:3] == ["ilm", "rule", "rm"]:
    state["rules"] = []
    mutation(["ilm rm"])
    save()
elif mc[:3] == ["ilm", "rule", "add"]:
    assert mc[3:7] == ["--noncurrent-expire-days", "1", "--expire-delete-marker", mc[6]], mc
    state["rules"].append({"Expiration": {"ExpiredObjectDeleteMarker": True}, "ID": "x",
                           "NoncurrentVersionExpiration": {"NoncurrentDays": 1}, "Status": "Enabled"})
    mutation(["ilm add"])
    save()
else:
    print("unexpected mc call: %s" % mc, file=sys.stderr)
    sys.exit(97)
"""

WANTED_RULE = {
    "Expiration": {"ExpiredObjectDeleteMarker": True},
    "ID": "existing",
    "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
    "Status": "Enabled",
}


@unittest.skipIf(ANSIBLE_PLAYBOOK is None, "ansible-playbook niedostepny w PATH")
class BucketProtectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="minio-protection-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        bindir = self.root / "bin"
        bindir.mkdir()
        docker = bindir / "docker"
        docker.write_text(FAKE_DOCKER, encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        self.state = self.root / "mc-state.json"
        self.env = dict(os.environ)
        self.env.update({
            "PATH": f"{bindir}{os.pathsep}{self.env.get('PATH', '')}",
            "FAKE_MC_STATE": str(self.state),
            "FAKE_MC_IMAGE": MC_IMAGE,
            "ANSIBLE_NOCOLOR": "1",
            "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible-tmp"),
            "HOME": str(self.root),
        })
        (self.root / "inventory.yml").write_text(
            yaml.safe_dump({"all": {"hosts": {"infra1": {"ansible_connection": "local", "ansible_become": False}}}}),
            encoding="utf-8",
        )
        (self.root / "play.yml").write_text(
            yaml.safe_dump([{
                "hosts": "infra1",
                "gather_facts": False,
                "tasks": [{"ansible.builtin.include_tasks": str(TASKS)}],
            }]),
            encoding="utf-8",
        )

    def run_tasks(self, s3_extra=None, retention_days=7):
        s3 = {"endpoint": "10.0.0.1:9000", "bucket": "tenant-backups"}
        s3.update(s3_extra or {})
        variables = {
            "backup": {"retention_days": retention_days, "s3": s3},
            "lock": {"minio": {"mc_image": "mc", "mc_image_digest": "sha256:test"}},
            "galera_backup_minio_workspace": {"path": str(self.root)},
        }
        extra = self.root / "vars.yml"
        extra.write_text(yaml.safe_dump(variables), encoding="utf-8")
        for suffix in (".calls", ".mutations"):
            Path(str(self.state) + suffix).unlink(missing_ok=True)
        result = subprocess.run(
            [ANSIBLE_PLAYBOOK, "-i", str(self.root / "inventory.yml"), str(self.root / "play.yml"),
             "-e", f"@{extra}"],
            cwd=str(REPO), capture_output=True, text=True, timeout=300, env=self.env,
        )
        return result, self._lines(".calls"), self._lines(".mutations")

    def _lines(self, suffix):
        path = Path(str(self.state) + suffix)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def given(self, **state):
        self.state.write_text(json.dumps({"locked": True, "rules": [], **state}), encoding="utf-8")

    def test_new_locked_bucket_gets_governance_retention_and_one_rule_then_converges(self):
        self.given()
        result, _, mutations = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(mutations, [["retention set", "governance", "7d"], ["ilm add"]])

        result, _, mutations = self.run_tasks()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(mutations, [], "drugi przebieg na zbieznym buckecie zmienil stan")

    def test_existing_bucket_without_lock_is_refused_before_any_change(self):
        self.given(locked=False)
        result, _, mutations = self.run_tasks()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("nie ma Object Lock", result.stdout)
        self.assertEqual(mutations, [])

    def test_explicit_opt_out_leaves_the_bucket_untouched(self):
        self.given(locked=False, rules=[{"Status": "Enabled", "ID": "operator"}])
        result, calls, _ = self.run_tasks({"object_lock": False})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls, [], "object_lock: false nie moze czytac ani zmieniac retencji i ILM")

    def test_unexpected_retention_read_error_fails_closed(self):
        self.given(retention_error=True)
        result, _, mutations = self.run_tasks()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Nie udalo sie odczytac retencji", result.stdout)
        self.assertEqual(mutations, [])

    def test_changed_retention_days_updates_the_default_lock(self):
        self.given(mode="GOVERNANCE", validity="7DAYS", rules=[WANTED_RULE])
        result, _, mutations = self.run_tasks(retention_days=14)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(mutations, [["retention set", "governance", "14d"]])

    def test_divergent_lifecycle_rules_are_replaced_by_exactly_one(self):
        foreign = {"Expiration": {"Days": 30}, "Filter": {"Prefix": "galera-"}, "ID": "old", "Status": "Enabled"}
        for rules in ([foreign], [WANTED_RULE, foreign], [dict(WANTED_RULE, Status="Disabled")]):
            with self.subTest(rules=rules):
                self.given(mode="GOVERNANCE", validity="7DAYS", rules=rules)
                result, _, mutations = self.run_tasks()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(mutations, [["ilm rm"], ["ilm add"]])
                self.assertEqual(json.loads(self.state.read_text())["rules"][0]["NoncurrentVersionExpiration"],
                                 {"NoncurrentDays": 1})


if __name__ == "__main__":
    unittest.main()
