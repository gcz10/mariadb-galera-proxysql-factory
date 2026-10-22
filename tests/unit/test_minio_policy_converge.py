#!/usr/bin/env python3
"""Konwergencja polityki konta MinIO zapisuje tylko przy roznicy.

Do 2026-10-21 `mc admin accesskey edit` szedl przy kazdym `configure`, wiec
przebieg bez zmian raportowal zmiane. Teraz zadanie porownuje polityke i nazwe
z odpowiedzi `mc admin accesskey info --json` z polityka zadeklarowana. Test
uruchamia DOKLADNIE kod porownania z zadania roli na odpowiedziach w ksztalcie
zmierzonym na zywym MinIO (RELEASE.2025-09-07: inna kolejnosc akcji).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
RECONCILE = REPO / "roles" / "galera_backup" / "tasks" / "reconcile_minio_account.yml"
NAME = "galera-backup-c1"

DECLARED = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
         "Resource": ["arn:aws:s3:::b"]},
        {"Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload",
                    "s3:ListMultipartUploadParts"],
         "Resource": ["arn:aws:s3:::b/galera-c1-*"]},
    ],
}


def compare_script():
    tasks = yaml.safe_load(RECONCILE.read_text(encoding="utf-8"))
    task = next(t for t in tasks if t.get("name") == "Compare the existing policy and name with the declared ones")
    argv = task["ansible.builtin.command"]["argv"]
    assert argv[:2] == ["/usr/bin/python3", "-c"]
    return argv[2]


def live_shape(policy, name=NAME, status="success"):
    """Odpowiedz `info --json` jak z MinIO: akcje i instrukcje w innej kolejnosci."""
    reordered = {
        "Version": policy["Version"],
        "Statement": [
            {**s, "Action": list(reversed(s["Action"]))} for s in reversed(policy["Statement"])
        ],
    }
    return json.dumps({"status": status, "parentUser": "root", "accountStatus": "on",
                       "policy": reordered, "name": name, "provider": "builtin"})


class PolicyConvergeComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = compare_script()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.policy_path = Path(cls.tmp.name) / "policy.json"
        cls.policy_path.write_text(json.dumps(DECLARED), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def verdict(self, stdin, name=NAME):
        proc = subprocess.run(
            [sys.executable, "-c", self.script, str(self.policy_path), name],
            input=stdin, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_same_policy_in_minio_order_needs_no_write(self):
        self.assertEqual(self.verdict(live_shape(DECLARED)), "same")

    def test_missing_permission_is_a_difference(self):
        narrowed = json.loads(json.dumps(DECLARED))
        narrowed["Statement"][1]["Action"].remove("s3:PutObject")
        self.assertEqual(self.verdict(live_shape(narrowed)), "differs")

    def test_extra_permission_is_a_difference(self):
        widened = json.loads(json.dumps(DECLARED))
        widened["Statement"][1]["Action"].append("s3:DeleteObject")
        self.assertEqual(self.verdict(live_shape(widened)), "differs")

    def test_renamed_account_is_a_difference(self):
        self.assertEqual(self.verdict(live_shape(DECLARED, name="someone-else")), "differs")

    def test_unreadable_or_failed_info_is_never_treated_as_converged(self):
        for stdin in ("", "not json", live_shape(DECLARED, status="error")):
            with self.subTest(stdin=stdin[:20]):
                self.assertEqual(self.verdict(stdin), "differs")


if __name__ == "__main__":
    unittest.main()
