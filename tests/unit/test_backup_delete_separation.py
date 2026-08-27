#!/usr/bin/env python3
"""Prawo KASOWANIA kopii off-cluster jest oddzielone od prawa ich zapisu.

POWSTAL PO ZMIANIE ARCHITEKTURY, NIE PO AWARII. Elekcja donora (P2-7) rozstawila
runnera i `secrets.env` na KAZDYM wezle Galery — wczesniej poswiadczenie MinIO
lezalo na jednym przypietym hoscie. Dopoki jedna polityka laczy `s3:PutObject`
z `s3:DeleteObject`, kompromitacja DOWOLNEGO wezla bazy pozwala skasowac cala
historie kopii off-cluster. Poufnosc danych live tego nie lagodzi: backup jest
ostatnia linia obrony i musi przezyc wlasny klaster.

Kontrakt pilnowany tutaj:
  1. poswiadczenie donora (na kazdym wezle) NIE MA zadnej akcji `s3:Delete*`,
  2. osobne poswiadczenie retencji kasuje wylacznie wlasny prefiks klastra
     i NIE MA prawa zapisu — kompromitacja koordynatora nie podmienia kopii,
  3. retencja NIGDY nie biegnie na backendzie zbudowanym z poswiadczen zapisu,
  4. wezel bez poswiadczenia retencji po prostu jej nie robi (to nie jest awaria
     — dokladnie jeden host w klastrze jest koordynatorem retencji),
  5. Ansible wysyla poswiadczenie retencji WYLACZNIE na host koordynatora,
     a derejestracja odwoluje OBA konta serwisowe, nie tylko konto zapisu.

Kazdy test jest falsyfikowalny: przywrocenie `s3:DeleteObject` do polityki
donora, wyciek poswiadczenia retencji do secrets.env na zwyklym wezle albo
powrot `backend.prune()` do sciezki backupu natychmiast go wywala.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from jinja2 import Template

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "roles" / "galera_backup" / "files"))

from galera_backup import pipeline  # noqa: E402
from galera_backup.secrets import (  # noqa: E402
    REDACT_ONLY_SECRET_KEYS,
    SENSITIVE_SECRET_KEYS,
)

ROLE = REPO / "roles" / "galera_backup"
WRITE_POLICY = ROLE / "templates" / "minio-policy.json.j2"
PRUNE_POLICY = ROLE / "templates" / "minio-policy-prune.json.j2"
SECRETS_TEMPLATE = ROLE / "templates" / "secrets.env.j2"

BUCKET = "green-galera-backups"
CLUSTER = "green-r9"
COORDINATOR = "grg1"
OTHER_NODE = "grg2"

WRITE_KEY_NAME = f"galera-backup-{CLUSTER}"
PRUNE_KEY_NAME = f"galera-backup-prune-{CLUSTER}"


def render_policy(template_path):
    return json.loads(
        Template(template_path.read_text(encoding="utf-8")).render(
            backup={"s3": {"bucket": BUCKET}},
            cluster={"name": CLUSTER},
        )
    )


def allowed_actions(policy, resource_arn):
    """Akcje dozwolone dla DOKLADNIE tego ARN-u (bez dopasowania wildcard)."""
    actions = set()
    for stmt in policy["Statement"]:
        if stmt.get("Effect") != "Allow":
            continue
        if resource_arn in stmt.get("Resource", []):
            actions.update(stmt.get("Action", []))
    return actions


def every_action(policy):
    actions = set()
    for stmt in policy["Statement"]:
        actions.update(stmt.get("Action", []))
    return actions


def resources_for(policy, action):
    resources = set()
    for stmt in policy["Statement"]:
        if action in stmt.get("Action", []):
            resources.update(stmt.get("Resource", []))
    return resources


class TestDonorCredentialCannotDelete(unittest.TestCase):
    """Polityka lezaca na KAZDYM wezle Galery nie moze kasowac historii."""

    def test_write_policy_grants_no_delete_action(self):
        deletes = sorted(
            a for a in every_action(render_policy(WRITE_POLICY)) if a.startswith("s3:Delete")
        )
        self.assertEqual(
            deletes,
            [],
            "poswiadczenie donora ma prawo kasowania — kompromitacja jednego "
            f"wezla bazy kasuje historie off-cluster: {deletes}",
        )

    def test_write_policy_keeps_publication_grants(self):
        """Odebranie delete nie moze po cichu zepsuc publikacji."""
        policy = render_policy(WRITE_POLICY)
        prefix_arn = f"arn:aws:s3:::{BUCKET}/galera-{CLUSTER}-*"
        actions = allowed_actions(policy, prefix_arn)
        for required in ("s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"):
            self.assertIn(required, actions, f"publikacja stracila {required}")
        self.assertIn("s3:ListBucket", allowed_actions(policy, f"arn:aws:s3:::{BUCKET}"))
        self.assertIn(
            "s3:GetObject",
            allowed_actions(policy, f"arn:aws:s3:::{BUCKET}/galera-backup-owner.json"),
        )


class TestRetentionCredentialPolicy(unittest.TestCase):
    """Osobne poswiadczenie retencji: kasuje wasko, nie pisze."""

    def test_prune_policy_exists(self):
        self.assertTrue(
            PRUNE_POLICY.exists(),
            "brak szablonu polityki retencji — delete nie zostal rozdzielony",
        )

    def test_delete_is_scoped_to_own_cluster_prefix(self):
        resources = resources_for(render_policy(PRUNE_POLICY), "s3:DeleteObject")
        self.assertEqual(
            resources,
            {f"arn:aws:s3:::{BUCKET}/galera-{CLUSTER}-*"},
            "retencja kasuje poza prefiksem wlasnego klastra",
        )

    def test_retention_credential_cannot_write(self):
        actions = every_action(render_policy(PRUNE_POLICY))
        self.assertNotIn(
            "s3:PutObject",
            actions,
            "poswiadczenie retencji moze nadpisac kopie — to ta sama klasa "
            "ryzyka co delete, tylko cichsza",
        )

    def test_retention_credential_can_verify_ownership_and_read_metadata(self):
        """Prune czyta metadata.json i marker ownera, zanim cokolwiek skasuje."""
        policy = render_policy(PRUNE_POLICY)
        self.assertIn("s3:ListBucket", allowed_actions(policy, f"arn:aws:s3:::{BUCKET}"))
        self.assertIn(
            "s3:GetObject",
            allowed_actions(policy, f"arn:aws:s3:::{BUCKET}/galera-{CLUSTER}-*"),
        )
        self.assertIn(
            "s3:GetObject",
            allowed_actions(policy, f"arn:aws:s3:::{BUCKET}/galera-backup-owner.json"),
        )


class TestSecretClassification(unittest.TestCase):
    def test_prune_secret_gates_argv_and_access_key_is_redacted(self):
        self.assertIn("GALERA_BACKUP_S3_PRUNE_SECRET_KEY", SENSITIVE_SECRET_KEYS)
        self.assertIn("GALERA_BACKUP_S3_PRUNE_ACCESS_KEY", REDACT_ONLY_SECRET_KEYS)
        self.assertNotIn(
            "GALERA_BACKUP_S3_PRUNE_ACCESS_KEY",
            SENSITIVE_SECRET_KEYS,
            "identyfikator w zbiorze bramkujacym argv wywala wlasne komendy runnera",
        )


def s3_config(**overrides):
    cfg = MagicMock()
    cfg.cluster_name = CLUSTER
    cfg.retention_days = 14
    cfg.backend = {
        "type": "s3",
        "endpoint": "192.168.1.47:9000",
        "bucket": BUCKET,
        "secure": False,
    }
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


WRITE_SECRETS = {
    "GALERA_BACKUP_S3_ACCESS_KEY": "write-ak",
    "GALERA_BACKUP_S3_SECRET_KEY": "write-sk",
}
COORDINATOR_SECRETS = dict(
    WRITE_SECRETS,
    GALERA_BACKUP_S3_PRUNE_ACCESS_KEY="prune-ak",
    GALERA_BACKUP_S3_PRUNE_SECRET_KEY="prune-sk",
)


class TestBackendFactorySeparatesPurposes(unittest.TestCase):
    def test_retention_backend_uses_the_retention_credential(self):
        with patch.object(pipeline, "S3Backend") as fake_cls:
            pipeline.get_storage_backend(
                s3_config(), COORDINATOR_SECRETS, None, purpose="retention"
            )
        kwargs = fake_cls.call_args.kwargs
        self.assertEqual(kwargs["access_key"], "prune-ak")
        self.assertEqual(kwargs["secret_key"], "prune-sk")

    def test_write_backend_never_receives_the_retention_credential(self):
        with patch.object(pipeline, "S3Backend") as fake_cls:
            pipeline.get_storage_backend(s3_config(), COORDINATOR_SECRETS, None)
        kwargs = fake_cls.call_args.kwargs
        self.assertEqual(kwargs["access_key"], "write-ak")
        self.assertEqual(kwargs["secret_key"], "write-sk")

    def test_retention_backend_fails_closed_without_its_credential(self):
        with patch.object(pipeline, "S3Backend") as fake_cls:
            with self.assertRaises(pipeline.BackupError) as ctx:
                pipeline.get_storage_backend(
                    s3_config(), WRITE_SECRETS, None, purpose="retention"
                )
        self.assertEqual(ctx.exception.code, "E_SECRETS")
        fake_cls.assert_not_called()


class TestRetentionExecution(unittest.TestCase):
    """`run_retention` jest jedynym miejscem, ktore kasuje kopie."""

    def setUp(self):
        self.events = MagicMock()

    def _emitted(self):
        return [call.args[0] for call in self.events.emit.call_args_list]

    def test_node_without_retention_credential_does_not_prune(self):
        write_backend = MagicMock()
        with patch.object(pipeline, "get_storage_backend") as factory:
            pipeline.run_retention(
                s3_config(), WRITE_SECRETS, None, self.events, "s3", write_backend
            )
        factory.assert_not_called()
        write_backend.prune.assert_not_called()
        self.assertEqual(self._emitted(), [])

    def test_coordinator_prunes_on_a_dedicated_backend(self):
        write_backend = MagicMock()
        prune_backend = MagicMock()
        prune_backend.prune.return_value = 2
        with patch.object(
            pipeline, "get_storage_backend", return_value=prune_backend
        ) as factory:
            pipeline.run_retention(
                s3_config(), COORDINATOR_SECRETS, None, self.events, "s3", write_backend
            )

        self.assertEqual(factory.call_args.kwargs.get("purpose"), "retention")
        write_backend.prune.assert_not_called()
        prune_backend.preflight.assert_called_once()
        self.assertEqual(prune_backend.prune.call_args.args[1], 14)
        self.assertIn("retention.success", self._emitted())

    def test_prune_failure_is_reported_and_never_raises(self):
        prune_backend = MagicMock()
        prune_backend.prune.side_effect = pipeline.BackupError("E_STORAGE", "denied")
        with patch.object(pipeline, "get_storage_backend", return_value=prune_backend):
            pipeline.run_retention(
                s3_config(), COORDINATOR_SECRETS, None, self.events, "s3", MagicMock()
            )
        self.assertIn("retention.failure", self._emitted())

    def test_non_s3_backend_keeps_pruning_on_its_own_mount(self):
        """SMB/filesystem nie maja rozdzielonych poswiadczen — kontrakt bez zmian."""
        backend = MagicMock()
        backend.prune.return_value = 0
        cfg = s3_config()
        cfg.backend = {"type": "filesystem", "mount_point": "/srv/backups"}
        with patch.object(pipeline, "get_storage_backend") as factory:
            pipeline.run_retention(cfg, WRITE_SECRETS, None, self.events, "filesystem", backend)
        factory.assert_not_called()
        backend.prune.assert_called_once()
        self.assertIn("retention.success", self._emitted())


class TestSecretsDistribution(unittest.TestCase):
    """Poswiadczenie retencji nie moze wyciec na zwykly wezel Galery."""

    def _render(self, host):
        return Template(SECRETS_TEMPLATE.read_text(encoding="utf-8")).render(
            inventory_hostname=host,
            backup={"destination": "s3", "scheduler": {"host": COORDINATOR}},
            galera_backup_local_role="scheduler",
            galera_backup_proxysql_stats_user="isa_stats",
            galera_backup_proxysql_stats_password="stats-pw",
            galera_backup_resolved_shared_secrets={
                "encryption_key": "enc",
                "s3_access_key": "write-ak",
                "s3_secret_key": "write-sk",
                "s3_prune_access_key": "prune-ak",
                "s3_prune_secret_key": "prune-sk",
            },
        )

    def test_coordinator_receives_the_retention_credential(self):
        rendered = self._render(COORDINATOR)
        self.assertIn("GALERA_BACKUP_S3_PRUNE_ACCESS_KEY", rendered)
        self.assertIn("prune-sk", rendered)

    def test_other_galera_nodes_do_not(self):
        rendered = self._render(OTHER_NODE)
        self.assertNotIn("PRUNE", rendered)
        self.assertNotIn("prune-sk", rendered)
        self.assertIn("write-ak", rendered, "zwykly donor stracil poswiadczenie zapisu")


def load_tasks(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def flatten(tasks):
    out = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        out.append(task)
        for key in ("block", "always", "rescue"):
            out.extend(flatten(task.get(key)))
    return out


class TestMinioProvisioning(unittest.TestCase):
    def setUp(self):
        self.provision = flatten(load_tasks(ROLE / "tasks" / "provision_minio.yml"))
        self.deregister = flatten(load_tasks(ROLE / "tasks" / "deregister_minio.yml"))

    def _argv_tasks(self, tasks):
        return [
            t["ansible.builtin.command"]["argv"]
            for t in tasks
            if isinstance(t.get("ansible.builtin.command"), dict)
            and "argv" in t["ansible.builtin.command"]
        ]

    def test_retention_account_is_created_under_its_own_name(self):
        names = {
            argv[argv.index("--name") + 1]
            for argv in self._argv_tasks(self.provision)
            if "--name" in argv
        }
        # Nazwa retencyjna MUSI pochodzi od nazwy klastra i MUSI przechodzic
        # przez `minio_service_account_name`: MinIO odrzuca nazwy > 32 znakow,
        # a filtr gwarantuje te sama, deterministyczna nazwe przy derejestracji
        # (selekcja przez minio_access_keys_named nizej). Surowe
        # `galera-backup-prune-{{ cluster.name }}` rozjonaloby sie od najemcy
        # o dluzszej nazwie — zmierzone 2026-08-27.
        self.assertTrue(
            any("minio_service_account_name" in name for name in names),
            f"konto retencji nie uzywa ograniczonej nazwy; znalezione: {names}",
        )
        self.assertIn("galera-backup-{{ cluster.name }}", names)

    def test_retention_account_gets_the_retention_policy(self):
        policies = set()
        for task in self.provision:
            template = task.get("ansible.builtin.template")
            if isinstance(template, dict):
                policies.add(template.get("src"))
        self.assertIn("minio-policy-prune.json.j2", policies)
        self.assertIn("minio-policy.json.j2", policies)

    def test_deregistration_revokes_both_accounts(self):
        selections = " ".join(
            str(task.get("ansible.builtin.set_fact", ""))
            for task in self.deregister
        )
        self.assertIn("'galera-backup-' ~ cluster.name", selections)
        self.assertIn("'galera-backup-prune-' ~ cluster.name", selections)


if __name__ == "__main__":
    unittest.main()
