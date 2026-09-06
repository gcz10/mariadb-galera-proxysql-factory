"""Behavioral tests for the ISC-21 static gate (tests/lab/probe-drift.py).

The probe must catch state-changing ProxySQL SQL (multiword SAVE/LOAD forms,
ADMIN VARIABLES, DML) in any YAML scalar — including multiline
command/argv/stdin payloads — while accepting a read-only playbook whose
SELECT statements or comments merely mention a mutating verb.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = WORKSPACE_ROOT / "tests" / "lab" / "probe-drift.py"
ENV_OVERRIDE = "CLUSTER_DRIFT_PLAYBOOK"


def _run_probe(playbook_path=None):
    env = os.environ.copy()
    if playbook_path is None:
        env.pop(ENV_OVERRIDE, None)
    else:
        env[ENV_OVERRIDE] = str(playbook_path)
    return subprocess.run(
        [sys.executable, str(PROBE_PATH)],
        cwd=WORKSPACE_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


def _contract_playbook():
    """Minimal playbook satisfying the probe's structural contract."""
    return [
        {
            "name": "fixture — ISC-21 structural contract",
            "hosts": "proxysql",
            "gather_facts": False,
            "tasks": [
                {
                    "name": "read-only main vs disk compare",
                    "ansible.builtin.shell": (
                        "mariadb -h127.0.0.1 -P6032 -uadmin -N -B -e "
                        '"SELECT hostgroup_id,hostname,port FROM mysql_servers ORDER BY 1,2"'
                    ),
                    "changed_when": False,
                },
                {
                    "name": "disk table pairs and galera uuid consistency",
                    "ansible.builtin.debug": {
                        "msg": (
                            "disk.mysql_servers disk.mysql_galera_hostgroups "
                            "disk.mysql_users wsrep_cluster_state_uuid"
                        )
                    },
                },
            ],
        }
    ]


def _shell_task(payload):
    return {
        "name": "fixture payload",
        "ansible.builtin.shell": payload,
        "changed_when": False,
    }


class ProbeDriftStaticGateTests(unittest.TestCase):
    def _run_with_task(self, task):
        playbook = _contract_playbook()
        playbook[0]["tasks"].append(task)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "f13_fixture.yml"
            path.write_text(yaml.safe_dump(playbook), encoding="utf-8")
            return _run_probe(path)

    def test_real_drift_playbook_passes(self):
        """Nienaruszony f13_drift.yml przechodzi bramkę."""
        result = _run_probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
        # PASS describes the static contract only — no runtime/falsification claim
        self.assertIn("static contract", result.stdout)
        self.assertNotIn("falsifiable", result.stdout)
        self.assertNotIn("FAIL", result.stdout)

    def test_canonical_two_word_save_and_load_fail(self):
        mutations = [
            "SAVE MYSQL SERVERS TO DISK",
            "LOAD MYSQL SERVERS TO RUNTIME",
            "SAVE MYSQL USERS TO DISK",
            "LOAD MYSQL USERS FROM DISK",
            "SAVE MYSQL VARIABLES TO DISK",
            "LOAD MYSQL VARIABLES TO RUNTIME",
            "SAVE ADMIN VARIABLES TO DISK",
            "LOAD ADMIN VARIABLES TO RUNTIME",
            # legacy one-word qualifier forms must keep failing
            "SAVE SERVERS TO DISK",
            # DML detection preserved
            "INSERT INTO mysql_servers VALUES(1,'gnode1',1)",
            "DELETE FROM mysql_servers",
            "UPDATE mysql_servers SET weight=2",
        ]
        for payload in mutations:
            with self.subTest(payload=payload):
                result = self._run_with_task(_shell_task(payload))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("state-changing", result.stdout)
                self.assertIn("ISC-21", result.stdout)

    def test_multiline_shell_block_payload_fails(self):
        payload = (
            "set -o pipefail\n"
            'mariadb -h127.0.0.1 -P6032 -uadmin -e "SAVE MYSQL\n'
            'SERVERS TO DISK"'
        )
        result = self._run_with_task(_shell_task(payload))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SAVE MYSQL", result.stdout)

    def test_multiline_argv_payload_fails(self):
        task = {
            "name": "fixture argv payload",
            "ansible.builtin.command": {
                "argv": ["mariadb", "-e", "LOAD MYSQL\nSERVERS TO RUNTIME"],
            },
            "changed_when": False,
        }
        result = self._run_with_task(task)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_multiline_stdin_payload_fails(self):
        task = {
            "name": "fixture stdin payload",
            "ansible.builtin.shell": {
                "stdin": "SAVE ADMIN\nVARIABLES TO DISK\n",
            },
            "changed_when": False,
        }
        result = self._run_with_task(task)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_read_only_select_mentioning_verbs_passes(self):
        payload = (
            "# runbook note: SAVE MYSQL SERVERS TO DISK belongs to the patch playbook\n"
            "mariadb -h127.0.0.1 -P6032 -uadmin -N -B -e "
            '"SELECT \'SAVE MYSQL SERVERS TO DISK\' AS saved_state FROM dual"\n'
            "mariadb -h127.0.0.1 -P6032 -uadmin -N -B -e "
            '"SELECT \'LOAD ADMIN VARIABLES TO RUNTIME\' AS note"\n'
            "-- history: DELETE FROM mysql_servers was never run here\n"
            'mariadb -h127.0.0.1 -P6032 -uadmin -N -B -e "SELECT 1"'
        )
        result = self._run_with_task(_shell_task(payload))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
