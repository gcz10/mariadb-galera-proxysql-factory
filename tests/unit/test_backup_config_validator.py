import os
import sys
import tempfile
import unittest
import subprocess
import yaml
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


class BackupConfigValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.clusters_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_cluster_pair(
        self,
        name: str,
        cluster_data: dict,
        inventory_data: dict,
    ) -> Path:
        dir_path = self.clusters_dir / name
        dir_path.mkdir(parents=True, exist_ok=True)
        with open(dir_path / "cluster.yml", "w") as f:
            yaml.dump(cluster_data, f)
        with open(dir_path / "inventory.yml", "w") as f:
            yaml.dump(inventory_data, f)
        return dir_path

    def validate(self) -> subprocess.CompletedProcess:
        script = WORKSPACE_ROOT / "tests" / "validation" / "validate-backup-config.py"
        return subprocess.run(
            [sys.executable, str(script), str(self.clusters_dir)],
            capture_output=True,
            text=True,
        )

    def valid_s3_cluster(
        self,
        name="test-cluster",
        env="laboratory",
        scheduler_host="gnode4",
        endpoint="192.168.1.47:9000",
        bucket="test-bucket",
        secure=False,
        pmm_name="test-galera",
    ) -> dict:
        return {
            "cluster": {
                "name": name,
                "environment": env,
                "profile": env,
                "automation_release": "1.0",
            },
            "platform": {"virtualization": "proxmox", "rocky_linux_major": 10},
            "versions": {"policy": "locked", "lock_file": "versions/versions-el10.lock.yml"},
            "galera": {"nodes_expected": 3},
            "proxysql": {
                "nodes_expected": 2,
                "read_write_split_enabled": False,
                "max_writers": 1,
                "endpoint": {"type": "keepalived_vip"},
            },
            "tls": {"mode": "disabled"},
            "network": {
                "application_cidrs": ["10.0.0.0/8"],
                "database_cluster_cidrs": ["192.168.1.0/24"],
                "administration_cidrs": ["192.168.1.0/24"],
                "monitoring_cidrs": ["192.168.1.0/24"],
            },
            "secrets": {"backend": "vault"},
            "storage": {"engine": "innodb"},
            "availability": {
                "rpo": "0",
                "rto_node_failure": "2m",
                "rto_full_cluster_failure": "30m",
                "maintenance_window": "weekend",
                "allowed_service_interruption": "2m",
            },
            "backup": {
                "enabled": True,
                "destination": "s3",
                "full_backup_schedule": "0 2 * * *",
                "incremental_backup_schedule": "disabled",
                "retention_days": 14,
                "encryption_enabled": True,
                "immutable_or_offsite_copy": True,
                "restore_test_schedule": "0 4 * * 0",
                "scheduler": {
                    "mode": "cron",
                    "host": scheduler_host,
                    "timezone": "UTC",
                },
                "s3": {
                    "endpoint": endpoint,
                    "bucket": bucket,
                    "region": "us-east-1",
                    "secure": secure,
                },
            },
            "monitoring": {
                "pmm": {
                    "server_url": "https://192.168.1.47",
                    "agent_id": "pmm-server",
                    "cluster_name": pmm_name,
                    "validate_certs": False,
                    "credentials_revision": 1,
                },
                "system": "pmm",
                "log_destination": "journald",
            },
        }

    def valid_inventory(self, hosts=None) -> dict:
        if hosts is None:
            hosts = ["gnode4", "gnode5", "gnode6"]
        hosts_dict = {h: {"ansible_host": f"192.168.1.{i+10}"} for i, h in enumerate(hosts)}
        return {
            "all": {
                "children": {
                    "galera": {
                        "hosts": hosts_dict,
                    }
                }
            }
        }

    def test_valid_s3_config_passes(self):
        c = self.valid_s3_cluster()
        inv = self.valid_inventory()
        self.create_cluster_pair("c1", c, inv)
        res = self.validate()
        self.assertEqual(res.returncode, 0, f"Stderr: {res.stderr}")

    def test_rejects_missing_scheduler(self):
        c = self.valid_s3_cluster()
        del c["backup"]["scheduler"]
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_scheduler_host_not_in_galera(self):
        c = self.valid_s3_cluster(scheduler_host="unknown-host")
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not in inventory group 'galera'", res.stderr)

    def test_rejects_malformed_cron(self):
        c = self.valid_s3_cluster()
        c["backup"]["full_backup_schedule"] = "invalid cron"
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("cron", res.stderr.lower())

    def test_rejects_non_disabled_incremental_schedule(self):
        c = self.valid_s3_cluster()
        c["backup"]["incremental_backup_schedule"] = "0 3 * * *"
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_encryption_disabled(self):
        c = self.valid_s3_cluster()
        c["backup"]["encryption_enabled"] = False
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_mixed_destination_blocks(self):
        c = self.valid_s3_cluster()
        c["backup"]["smb"] = {
            "source": "//nas/share",
            "mount_point": "/mnt/backup",
            "options": ["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
        }
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("mixed destination", res.stderr.lower())

    def test_rejects_relative_mount_point(self):
        c = self.valid_s3_cluster()
        c["backup"]["destination"] = "smb"
        del c["backup"]["s3"]
        c["backup"]["smb"] = {
            "source": "//nas/share",
            "mount_point": "relative/path",
            "options": ["vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
        }
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_unsafe_smb_options(self):
        unsafe_cases = [
            ["username=admin", "vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            ["password=secret", "vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            ["credentials=/etc/smb", "vers=3.1.1", "seal", "nosuid", "nodev", "noexec"],
            ["vers=2.0", "seal", "nosuid", "nodev", "noexec"],
            ["noseal", "vers=3.1.1", "nosuid", "nodev", "noexec"],
        ]
        for options in unsafe_cases:
            c = self.valid_s3_cluster()
            c["backup"]["destination"] = "smb"
            del c["backup"]["s3"]
            c["backup"]["smb"] = {
                "source": "//nas/share",
                "mount_point": "/mnt/backup",
                "options": options,
            }
            self.create_cluster_pair(f"c_unsafe_{hash(str(options))}", c, self.valid_inventory())
            res = self.validate()
            self.assertNotEqual(res.returncode, 0, f"Options failed to trigger rejection: {options}")

    def test_rejects_missing_required_smb_options(self):
        c = self.valid_s3_cluster()
        c["backup"]["destination"] = "smb"
        del c["backup"]["s3"]
        c["backup"]["smb"] = {
            "source": "//nas/share",
            "mount_point": "/mnt/backup",
            "options": ["vers=3.1.1", "nosuid", "nodev", "noexec"],
        }
        self.create_cluster_pair("c_missing_opt", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_unsecure_s3_in_production(self):
        c = self.valid_s3_cluster(env="production", secure=False)
        c["cluster"]["profile"] = "production"
        c["versions"]["policy"] = "locked"
        self.create_cluster_pair("c1", c, self.valid_inventory())
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("secure", res.stderr.lower())

    def test_rejects_duplicate_normalized_s3_ownership(self):
        c1 = self.valid_s3_cluster(
            name="cluster1",
            pmm_name="pmm1",
            endpoint="HTTPS://s3.example.com:443/",
            bucket="orders",
            secure=True,
        )
        c2 = self.valid_s3_cluster(
            name="cluster2",
            pmm_name="pmm2",
            endpoint="s3.example.com",
            bucket="orders",
            secure=True,
        )
        inv = self.valid_inventory()
        self.create_cluster_pair("c1", c1, inv)
        self.create_cluster_pair("c2", c2, inv)
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("duplicate S3 ownership", res.stderr)

    def test_rejects_duplicate_cluster_names(self):
        c1 = self.valid_s3_cluster(name="dupe-cluster", pmm_name="pmm1", bucket="b1")
        c2 = self.valid_s3_cluster(name="dupe-cluster", pmm_name="pmm2", bucket="b2")
        inv = self.valid_inventory()
        self.create_cluster_pair("c1", c1, inv)
        self.create_cluster_pair("c2", c2, inv)
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)

    def test_rejects_duplicate_pmm_cluster_names(self):
        c1 = self.valid_s3_cluster(name="cluster1", pmm_name="dupe-pmm", bucket="b1")
        c2 = self.valid_s3_cluster(name="cluster2", pmm_name="dupe-pmm", bucket="b2")
        inv = self.valid_inventory()
        self.create_cluster_pair("c1", c1, inv)
        self.create_cluster_pair("c2", c2, inv)
        res = self.validate()
        self.assertNotEqual(res.returncode, 0)


if __name__ == "__main__":
    unittest.main()
