import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "lab"))

import _quorum_evidence as qe  # noqa: E402


def sample_record():
    acceptance = {name: True for name in qe.FINAL_ACCEPTANCE}
    return {
        "run_id": "a" * 32,
        "cluster": "newclaude16-r9",
        "timestamp_utc": "2026-08-21T12:00:00Z",
        "outcome": qe.OUTCOME_DEGRADED,
        "contract": {"expected": "degraded", "observed": "degraded", "match": True},
        "acceptance": acceptance,
        "errors": [],
        "versions": {
            "proxysql": {"fcp1": "3.0.10", "fcp2": "3.0.10"},
            "mariadb": "11.4.12-MariaDB-log",
            "client": "mariadb 11.4.12",
            "os_proxysql": {"fcp1": "Rocky Linux 10", "fcp2": "Rocky Linux 10"},
            "os_backend": "Rocky Linux 9.6",
        },
        "topology": {
            "vip": "192.168.1.135",
            "app_user": "app_user_n16",
            "galera": {"n16g1": "192.168.1.172", "n16g2": "192.168.1.173", "n16g3": "192.168.1.174"},
            "proxysql": {"fcp1": "192.168.1.131", "fcp2": "192.168.1.132"},
            "writer_hostgroup": 810,
            "offline_hostgroup": 840,
        },
        "baseline": {
            "vip_holder": "fcp1",
            "galera": {"n16g1": "Primary/3/4", "n16g2": "Primary/3/4", "n16g3": "Primary/3/4"},
            "runtime_writer": {"fcp1": {"hostgroup_id": 810, "hostname": "192.168.1.172", "status": "ONLINE"}},
            "app_write_ok": True,
        },
        "failure": {
            "window_started_utc": "2026-08-21T12:01:00Z",
            "window_ended_utc": "2026-08-21T12:01:03Z",
            "survivor": "n16g1",
            "stopped": ["n16g2", "n16g3"],
            "survivor_status": "non-Primary",
            "app_code": "2027",
            "app_sqlstate": "HY000",
            "app_error": "ERROR 2027 (HY000): Received malformed packet",
            "node_code": "1047",
            "node_sqlstate": "08S01",
            "node_error": "ERROR 1047 (08S01): WSREP has not yet prepared node for application use",
            "monitor_row": {
                "hostname": "192.168.1.172", "time_start_us": 1787313661000000,
                "primary_partition": "NO", "wsrep_local_state": 0, "error": "",
            },
            "runtime_survivor": {"hostgroup_id": 810, "hostname": "192.168.1.172", "status": "ONLINE"},
            "proxysql_node": "fcp1",
            "proxysql_log": "2026-08-21 error 1047 backend 192.168.1.172 WSREP has not yet prepared node",
        },
        "cleanup": {
            "nodes": {"n16g2": {"dropin_absent": True, "restart_policy_restored": True, "start_enqueued": True},
                      "n16g3": {"dropin_absent": True, "restart_policy_restored": True, "start_enqueued": True}},
            "credential_profile_absent": True,
        },
        "recovery": {
            "nodes": {"n16g1": "Primary/3/4", "n16g2": "Primary/3/4", "n16g3": "Primary/3/4"},
            "app_write_ok": True,
        },
        "external_gates": {
            "platform_verify": {"ok": True, "command": "make platform-verify", "rc": 0},
            "post_build_gate": {"ok": True, "command": "make lab-post-build-gate CLUSTER=newclaude16-r9", "rc": 0},
        },
    }


class ParsingTests(unittest.TestCase):
    def test_error_code_and_sqlstate(self):
        self.assertEqual(
            qe.parse_client_error("ERROR 1047 (08S01): WSREP has not yet prepared node"),
            ("1047", "08S01"),
        )

    def test_missing_sqlstate_is_not_inferred(self):
        self.assertEqual(qe.parse_client_error("ERROR 2027: malformed"), ("2027", qe.NO_ERROR))

    def test_tsv_rows_are_named_and_malformed_rows_fail(self):
        self.assertEqual(
            qe.parse_tsv("810\t192.0.2.1\tONLINE", ("hostgroup_id", "hostname", "status")),
            [{"hostgroup_id": "810", "hostname": "192.0.2.1", "status": "ONLINE"}],
        )
        with self.assertRaises(ValueError):
            qe.parse_tsv("810\tmissing", ("hostgroup_id", "hostname", "status"))


class ClassificationTests(unittest.TestCase):
    def test_degraded_clean_and_unresolved_are_disjoint(self):
        self.assertEqual(qe.classify_outcome("2027", "HY000", "1047", "08S01"), qe.OUTCOME_DEGRADED)
        self.assertEqual(qe.classify_outcome("1047", "08S01", "1047", "08S01"), qe.OUTCOME_CLEAN)
        self.assertEqual(qe.classify_outcome("2003", "HY000", "1047", "08S01"), qe.OUTCOME_CLEAN)
        self.assertEqual(qe.classify_outcome("2013", "HY000", "1047", "08S01"), qe.OUTCOME_UNRESOLVED)
        self.assertEqual(qe.classify_outcome("2027", "HY000", "1047", qe.NO_ERROR), qe.OUTCOME_UNRESOLVED)


class SafetyHelpersTests(unittest.TestCase):
    def test_option_file_quote_uses_documented_escapes(self):
        self.assertEqual(qe.option_file_quote('a b#c"\\\n\t'), '"a\\sb#c\\"\\\\\\n\\t"')

    def test_whole_cluster_recovery_is_exact(self):
        self.assertTrue(qe.recovery_complete({"a": "Primary/3/4", "b": "Primary/3/4", "c": "Primary/3/4"}, 3))
        self.assertFalse(qe.recovery_complete({"a": "Primary/2/4", "b": "Primary/2/4", "c": "?/?/?"}, 3))

    def test_log_proof_requires_error_and_survivor_address(self):
        self.assertTrue(qe.proxy_log_proves_backend_error("error 1047 backend 192.0.2.7", "192.0.2.7"))
        self.assertFalse(qe.proxy_log_proves_backend_error("error 1047 backend 192.0.2.8", "192.0.2.7"))
        self.assertFalse(qe.proxy_log_proves_backend_error("backend 192.0.2.7 timeout", "192.0.2.7"))


class AcceptanceAndRenderingTests(unittest.TestCase):
    def test_final_acceptance_is_fail_closed_and_lists_errors(self):
        record = sample_record()
        self.assertEqual(qe.acceptance_failures(record, final=True), [])
        record["acceptance"]["platform_verify"] = False
        record["errors"].append("platform gate failed")
        self.assertEqual(
            qe.acceptance_failures(record, final=True),
            ["platform_verify", "platform gate failed"],
        )
        rendered = qe.render_record(record)
        self.assertIn("platform gate failed", rendered)
        self.assertIn("platform_verify", rendered)

    def test_issue_requires_final_acceptance(self):
        record = sample_record()
        record["acceptance"]["post_build_gate"] = None
        with self.assertRaises(ValueError):
            qe.render_issue_draft(record)

    def test_issue_is_symbolic_and_uses_measured_runtime_row(self):
        draft = qe.render_issue_draft(sample_record())
        for leaked in ("192.168.1.", "n16g", "fcp", "app_user_n16", "newclaude16-r9"):
            self.assertNotIn(leaked, draft)
        self.assertIn("hostgroup 810", draft)
        self.assertIn("ONLINE", draft)
        self.assertIn("measured, not documented", draft)
        self.assertIn("sameness with #1596 is not established", draft)

    def test_issue_redacts_residual_unrelated_ips(self):
        record = sample_record()
        record["failure"]["proxysql_log"] = (
            "2026-08-21 error 1047 backend 192.168.1.172 from client 10.200.42.99 WSREP has not yet prepared node"
        )
        draft = qe.render_issue_draft(record)
        self.assertNotIn("10.200.42.99", draft)
        self.assertNotIn("192.168.1.172", draft)
        self.assertIn("[IP-REDACTED]", draft)
        self.assertIn("db1", draft)


if __name__ == "__main__":
    unittest.main()
