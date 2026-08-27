import json
import unittest
from pathlib import Path

from jsonschema import Draft7Validator

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = WORKSPACE_ROOT / "clusters" / "schema" / "cluster.schema.json"

# Kontrakt schema cluster.yml po usunieciu pol-widmo (2026-08-20):
# macierz reject/accept pilnuje, ze zadne pole bez konsumenta nie wraca,
# a parametry konsumowane przez role/playbooki nie sa ukryte poza schema.
#
# Konsumentow uwzglednionych w testach (grep 2026-08-20):
#   availability.rto_node_failure   tests/lab/chaos-failover.py:78,
#                                   tests/lab/chaos-proxysql-failover.py:140 (ISC-27)
#   storage.data_directory          playbooks/f2_preflight.yml (wolne miejsce na datadir)
#   storage.expected_database_size_gb playbooks/f2_preflight.yml (prog SST mariabackup)
#   mariadb_tuning.wsrep_slave_threads    roles/mariadb_install/templates/server.cnf.j2
#   mariadb_tuning.wsrep_log_conflicts    roles/mariadb_install/templates/server.cnf.j2


def canonical_cluster() -> dict:
    """Minimalny klaster zgodny z kontraktem: same pola z konsumentami."""
    return {
        "cluster": {
            "name": "contract-cluster",
            "environment": "laboratory",
            "profile": "laboratory",
        },
        "platform": {"rocky_linux_major": 9},
        "versions": {"policy": "locked", "lock_file": "versions/versions.lock.yml"},
        "galera": {"cluster_name": "contract_galera", "nodes_expected": 3},
        "proxysql": {
            "nodes_expected": 2,
            "endpoint": {"type": "keepalived_vip", "address": "10.0.0.100", "port": 6033},
        },
        "tls": {"mode": "disabled"},
        "network": {
            "application_cidrs": ["10.0.0.0/8"],
            "database_cluster_cidrs": ["10.0.0.0/8"],
            "administration_cidrs": ["10.0.0.0/8"],
            "monitoring_cidrs": ["10.0.0.0/8"],
        },
        "storage": {
            "data_directory": "/var/lib/mysql",
            "expected_database_size_gb": "unknown",
        },
        "availability": {"rto_node_failure": "2m"},
        "backup": {
            "enabled": True,
            "destination": "s3",
            "full_backup_schedule": "0 2 * * *",
            "incremental_backup_schedule": "disabled",
            "freshness_sla_hours": 26,
            "retention_days": 14,
            "encryption_enabled": True,
            "immutable_or_offsite_copy": True,
            "restore_test_schedule": "0 4 * * 0",
            "scheduler": {"mode": "cron", "host": "gnode1", "timezone": "UTC"},
            "s3": {
                "endpoint": "127.0.0.1:9000",
                "bucket": "contract-bucket",
                "region": "us-east-1",
                "secure": False,
            },
        },
        "monitoring": {
            "pmm": {
                "server_url": "https://127.0.0.1:8443",
                "agent_id": "pmm-server",
                "cluster_name": "contract-galera",
                "validate_certs": False,
                "credentials_revision": 1,
            }
        },
    }


# Pola-widma: nazwa + fragment bledu schema. Legacy fixture ponizej zawiera
# komplet dawnego kontraktu, wiec przed zmiana schema przechodzil walidacje;
# po zmianie kazde z tych pol musi byc jawnie odrzucone.
GHOST_FIELDS = [
    ("cluster.automation_release", "automation_release"),
    ("platform.virtualization", "virtualization"),
    ("secrets.backend", "secrets"),
    ("monitoring.system", "system"),
    ("storage.backup_staging_directory", "backup_staging_directory"),
    ("storage.filesystem", "filesystem"),
    ("storage.expected_growth_gb_per_month", "expected_growth_gb_per_month"),
    ("storage.available_iops", "available_iops"),
    ("availability.rpo", "rpo"),
    ("availability.rto_full_cluster_failure", "rto_full_cluster_failure"),
    ("availability.maintenance_window", "maintenance_window"),
    ("availability.allowed_service_interruption", "allowed_service_interruption"),
    ("proxysql.max_writers", "max_writers"),
    ("proxysql.read_write_split_enabled", "read_write_split_enabled"),
    ("tls.certificate_source", "certificate_source"),
]


def legacy_cluster() -> dict:
    """Dawny kompletny kontrakt, valid przed usunieciem pol-widmo."""
    cluster = canonical_cluster()
    cluster["cluster"]["automation_release"] = "0.1.0"
    cluster["platform"]["virtualization"] = "proxmox_kvm"
    cluster["secrets"] = {"backend": "ansible_vault"}
    cluster["monitoring"]["system"] = "pmm"
    cluster["storage"].update(
        {
            "backup_staging_directory": "/var/tmp/mariadb-backup",
            "filesystem": "xfs",
            "expected_growth_gb_per_month": "unknown",
            "available_iops": "unknown",
        }
    )
    cluster["availability"].update(
        {
            "rpo": "0",
            "rto_full_cluster_failure": "30m",
            "maintenance_window": "weekend",
            "allowed_service_interruption": "2m",
        }
    )
    cluster["proxysql"].update({"max_writers": 1, "read_write_split_enabled": False})
    cluster["tls"]["certificate_source"] = "file"
    return cluster


class ClusterSchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text())
        cls.validator = Draft7Validator(cls.schema)

    def assert_valid(self, cluster: dict):
        errors = sorted(self.validator.iter_errors(cluster), key=lambda e: list(e.absolute_path))
        if errors:
            msgs = "; ".join(
                f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
                for e in errors[:3]
            )
            self.fail(f"klaster kanoniczny odrzucony przez schema: {msgs}")

    def assert_invalid(self, cluster: dict, fragment: str = ""):
        errors = list(self.validator.iter_errors(cluster))
        if not errors:
            self.fail("schema zaakceptowala konfiguracje, ktora miala byc odrzucona")
        if fragment:
            joined = " | ".join(
                f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors
            )
            self.assertIn(fragment, joined)

    def test_canonical_minimal_cluster_is_valid(self):
        # Kanoniczne minimum (zero pol-widmo) MUSI przechodzic — inaczej
        # schema wymaga pol bez konsumentow.
        self.assert_valid(canonical_cluster())

    def test_ghost_fields_are_rejected(self):
        for name, fragment in GHOST_FIELDS:
            with self.subTest(field=name):
                self.assert_invalid(legacy_cluster(), fragment)

    def test_rto_node_failure_is_required(self):
        # Jedyny zywotny parametr availability: chaos-failover.py robi twarde
        # CLUSTER["availability"]["rto_node_failure"] (ISC-27). Bez sekcji
        # sonda odmawia startu — pole zostaje wymagane.
        cluster = canonical_cluster()
        del cluster["availability"]
        self.assert_invalid(cluster)

    def test_shadow_tuning_params_are_declared(self):
        # server.cnf.j2 czyta mariadb_tuning.wsrep_slave_threads i
        # wsrep_log_conflicts — dopoki ich nie bylo w schema, byly to pola
        # ukryte (additionalProperties:false je odrzucalo).
        cluster = canonical_cluster()
        cluster["mariadb_tuning"] = {"wsrep_slave_threads": 8, "wsrep_log_conflicts": "ON"}
        self.assert_valid(cluster)

    def test_shadow_tuning_params_reject_garbage(self):
        cluster = canonical_cluster()
        cluster["mariadb_tuning"] = {"wsrep_log_conflicts": "MAYBE"}
        self.assert_invalid(cluster, "wsrep_log_conflicts")

        cluster = canonical_cluster()
        cluster["mariadb_tuning"] = {"wsrep_slave_threads": 0}
        self.assert_invalid(cluster, "wsrep_slave_threads")

    def test_tls_full_without_certificate_source(self):
        # certificate_source byl polem-widmem: zero lookupow, jedyna wzmianka
        # to komentarz. mode=full wymaga wylacznie trojki *_reference.
        cluster = canonical_cluster()
        cluster["tls"] = {
            "mode": "full",
            "ca_reference": "pki/x/ca.pem",
            "certificate_reference": "pki/x/server-cert.pem",
            "private_key_reference": "pki/x/server-key.pem",
        }
        self.assert_valid(cluster)

    def test_real_clusters_validate(self):
        # Wszystkie realne cluster.yml trzymaja sie kontraktu po sprzataniu.
        # Katalogi archiwalne zadepozycono w docs/records/archives i nie licza
        # sie do puli; dolna granica 1 chroni przed pustym globem ukrywajacym
        # blad sciezki, a nie przed wymogiem konkretnej liczby klastrow.
        paths = sorted((WORKSPACE_ROOT / "clusters").glob("*/cluster.yml"))
        self.assertGreaterEqual(len(paths), 1)
        for path in paths:
            with self.subTest(cluster=path.parent.name):
                import yaml

                self.assert_valid(yaml.safe_load(path.read_text()))


class ConsumerGuardTests(unittest.TestCase):
    """Piny konsumentow — zabezpieczenie przed kolejnym dryfem kontraktu.

    Kazde polozone tu pole MA konsumenta w drzewie; jesli konsument znika,
    test puchnie i wymaga jawnej decyzji (usuniecie pola ze schema zamiast
    zostawiania martwego wymogu).
    """

    def read(self, rel: str) -> str:
        return (WORKSPACE_ROOT / rel).read_text()

    def test_rto_node_failure_consumed_by_chaos_probes(self):
        self.assertIn("rto_node_failure", self.read("tests/lab/chaos-failover.py"))
        self.assertIn("rto_node_failure", self.read("tests/lab/chaos-proxysql-failover.py"))

    def test_storage_fields_consumed_by_preflight(self):
        preflight = self.read("playbooks/f2_preflight.yml")
        self.assertIn("data_directory", preflight)
        self.assertIn("expected_database_size_gb", preflight)

    def test_wsrep_tuning_params_consumed_by_server_cnf(self):
        template = self.read("roles/mariadb_install/templates/server.cnf.j2")
        self.assertIn("wsrep_slave_threads", template)
        self.assertIn("wsrep_log_conflicts", template)


if __name__ == "__main__":
    unittest.main()
