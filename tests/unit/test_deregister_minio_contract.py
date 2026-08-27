#!/usr/bin/env python3
"""Kontrakt derejestracji zarzadzanego konta MinIO per klaster.

Derejestracja ma odwolac wyłącznie konto nazwane ``galera-backup-<cluster>``.
Bucket, owner marker i kopie pozostaja danymi operatora i nie sa usuwane.
"""

import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "cluster_deregister.yml"
ROLE_TASKS = REPO / "roles" / "galera_backup" / "tasks" / "deregister_minio.yml"
ROOT_ENV = REPO / "roles" / "galera_backup" / "tasks" / "minio_root_env.yml"
OWNED_KEYS = REPO / "roles" / "galera_backup" / "tasks" / "minio_owned_keys.yml"
STATIC_WORKSPACE = "/run/galera-backup-minio-tmp"
DYNAMIC_WORKSPACE = "{{ galera_backup_minio_workspace.path }}"
DYNAMIC_ROOT_ENV = f"{DYNAMIC_WORKSPACE}/root.env"




def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def flatten_tasks(tasks):
    flat = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        flat.append(task)
        for section in ("block", "rescue", "always"):
            flat.extend(flatten_tasks(task.get(section)))
    return flat


def find_task(tasks, fragment):
    return next(
        (task for task in tasks if fragment in task.get("name", "")),
        None,
    )


def collect_argv(node):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "argv" and isinstance(value, list):
                found.append(value)
            else:
                found.extend(collect_argv(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(collect_argv(value))
    return found

def collect_command_text(node):
    chunks = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("ansible.builtin.command", "ansible.builtin.shell"):
                chunks.append(yaml.safe_dump(value))
            else:
                chunks.extend(collect_command_text(value))
    elif isinstance(node, list):
        for value in node:
            chunks.extend(collect_command_text(value))
    return "\n".join(chunks)



class DeregisterMinioContractTests(unittest.TestCase):
    def setUp(self):
        self.plays = load_yaml(PLAYBOOK)

    def preflight_play(self):
        for play in self.plays:
            if find_task(play.get("tasks", []), "Preflight MinIO") is not None:
                return play
        self.fail("brak preflightu MinIO przed mutacjami derejestracji")

    def minio_play(self):
        for play in self.plays:
            for task in play.get("tasks", []):
                include = task.get("ansible.builtin.include_role", {})
                if include.get("tasks_from") == "deregister_minio.yml":
                    return play, task
        self.fail("cluster_deregister.yml nie wlacza roli deregister_minio.yml")

    def test_cleanup_runs_only_for_repository_managed_minio(self):
        preflight = self.preflight_play()
        predicate_task = find_task(
            preflight.get("tasks", []),
            "Ustal czy klaster korzysta z zarzadzanego MinIO",
        )
        self.assertIsNotNone(predicate_task)
        predicate = str(
            predicate_task["ansible.builtin.set_fact"][
                "galera_backup_managed_minio"
            ]
        )
        for required in (
            "backup.destination == 's3'",
            "groups['infra']",
            "backup.s3.endpoint",
            "hostvars[groups['infra'][0]].ansible_host",
        ):
            self.assertIn(required, predicate)
        self.assertNotIn(
            "backup.enabled",
            predicate,
            "wylaczenie backupu przed teardownem nie moze zostawic dawnego konta",
        )
        _, include = self.minio_play()
        self.assertIn("galera_backup_managed_minio", str(include.get("when", "")))

    def test_credentials_are_checked_before_any_mutating_play(self):
        preflight = self.preflight_play()
        self.assertIs(
            self.plays[0],
            preflight,
            "preflight MinIO musi poprzedzac PMM/Grafana/ProxySQL DELETE",
        )
        guard = find_task(preflight.get("tasks", []), "Preflight MinIO")
        self.assertIsNotNone(guard)
        self.assertIn("galera_backup_managed_minio", str(guard.get("when", "")))
        assertion = guard["ansible.builtin.assert"]
        self.assertTrue(assertion.get("quiet"))
        self.assertFalse(guard.get("no_log", False))

    def test_role_is_loaded_from_the_existing_backup_boundary(self):
        _, include = self.minio_play()
        spec = include["ansible.builtin.include_role"]
        self.assertEqual(spec.get("name"), "galera_backup")
        self.assertEqual(spec.get("tasks_from"), "deregister_minio.yml")


class DeregisterMinioRoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = load_yaml(ROLE_TASKS)
        cls.tasks = flatten_tasks(cls.document)

    def test_root_credentials_are_required_with_visible_diagnostic(self):
        # Srodowisko root jest wspolne z provisioningu (minio_root_env.yml),
        # wiec asercja istnieje w jednym egzemplarzu.
        root_tasks = flatten_tasks(load_yaml(ROOT_ENV))
        guard = find_task(root_tasks, "Wymagaj poswiadczen root MinIO")
        checks = guard["ansible.builtin.assert"]["that"]
        for variable in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
            self.assertTrue(
                any(variable in check and "length > 0" in check for check in checks)
            )
            self.assertTrue(
                any(variable in check and "is not search" in check for check in checks)
            )
        self.assertTrue(guard["ansible.builtin.assert"].get("quiet"))
        self.assertFalse(
            guard.get("no_log", False),
            "no_log ukrywa fail_msg, choc assert pokazuje tylko nazwy zmiennych",
        )
        allocation = find_task(root_tasks, "katalog tymczasowy klienta MinIO")
        self.assertIsNotNone(allocation, "brak unikalnego workspace MinIO")
        tempfile = allocation.get("ansible.builtin.tempfile", {})
        self.assertEqual(tempfile.get("state"), "directory")
        self.assertEqual(tempfile.get("path"), "/run")
        self.assertEqual(tempfile.get("prefix"), "galera-backup-minio-")
        self.assertEqual(
            allocation.get("register"), "galera_backup_minio_workspace"
        )
        self.assertTrue(allocation.get("become"))
        self.assertIn("groups['infra'][0]", str(allocation.get("delegate_to", "")))

        env_task = find_task(root_tasks, "Zapisz root-only srodowisko klienta MinIO")
        self.assertIsNotNone(env_task)
        copy = env_task["ansible.builtin.copy"]
        self.assertEqual(copy.get("mode"), "0600")
        self.assertIn("MC_HOST_myminio=", copy.get("content", ""))
        self.assertTrue(env_task.get("no_log"))
        # Derejestracja musi dzialac z localhosta — shared file sam dowozi
        # become i delegacje na infra.
        for shared_task in root_tasks:
            if "ansible.builtin.file" in shared_task or "ansible.builtin.copy" in shared_task:
                self.assertIn("groups['infra'][0]", str(shared_task.get("delegate_to", "")))
                self.assertTrue(shared_task.get("become"))
    def test_role_uses_only_invocation_workspace(self):
        for path in (ROLE_TASKS, ROOT_ENV, OWNED_KEYS):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                STATIC_WORKSPACE,
                text,
                f"{path.relative_to(REPO)} nadal wspoldzieli statyczny workspace",
            )



    def test_exact_named_service_accounts_are_selected_and_revoked(self):
        select = find_task(self.tasks, "Wybierz konta MinIO tego klastra")
        self.assertIsNotNone(select)
        expression = str(
            select["ansible.builtin.set_fact"]["galera_backup_deregister_access_keys"]
        )
        self.assertIn("minio_access_keys_named('galera-backup-' ~ cluster.name)", expression)

        revoke = find_task(self.tasks, "Odwolaj konta MinIO tego klastra")
        self.assertIsNotNone(revoke)
        argv = revoke["ansible.builtin.command"]["argv"]
        self.assertIn("admin", argv)
        self.assertIn("accesskey", argv)
        self.assertIn("remove", argv)
        self.assertEqual(
            revoke.get("loop"),
            "{{ galera_backup_deregister_access_keys | default([]) }}",
        )
        self.assertTrue(revoke.get("no_log"))
        self.assertEqual(revoke.get("changed_when"), True)

    def test_mc_commands_use_pinned_image_env_file_and_infra_host(self):
        # Discovery kont zyje teraz w minio_owned_keys.yml — kontrakt `mc`
        # sprawdzamy na unii derejestracji i wspolnego discovery.
        command_tasks = [
            task
            for document in (self.tasks, flatten_tasks(load_yaml(OWNED_KEYS)))
            for task in document
            if "ansible.builtin.command" in task
        ]
        self.assertGreaterEqual(len(command_tasks), 3)
        for task in command_tasks:
            argv = task["ansible.builtin.command"].get("argv", [])
            joined = " ".join(str(item) for item in argv)
            self.assertIn("docker run --rm", joined)
            self.assertIn("--network container:minio", joined)
            self.assertIn(f"--env-file {DYNAMIC_ROOT_ENV}", joined)
            self.assertIn("{{ lock.minio.mc_image }}@{{ lock.minio.mc_image_digest }}", argv)
            self.assertIn("groups['infra'][0]", str(task.get("delegate_to", "")))
            self.assertTrue(task.get("no_log"))

    def test_temporary_root_credentials_are_removed_even_on_failure(self):
        blocks = [task for task in self.document if task.get("always")]
        self.assertEqual(len(blocks), 1)
        cleanup = find_task(flatten_tasks(blocks[0]["always"]), "Usun tymczasowe poswiadczenia root MinIO")
        self.assertIsNotNone(cleanup)
        self.assertEqual(cleanup["ansible.builtin.file"].get("state"), "absent")
        self.assertEqual(
            cleanup["ansible.builtin.file"].get("path"),
            DYNAMIC_WORKSPACE,
        )
        self.assertIn("groups['infra'][0]", str(cleanup.get("delegate_to", "")))

    def test_bucket_and_backup_objects_are_not_deleted(self):
        for argv in collect_argv(self.document):
            self.assertNotIn("rb", argv)
            self.assertNotIn("rm", argv)

        command_text = collect_command_text(self.document)
        self.assertNotRegex(command_text, r"\bmc\s+(?:rb|rm|mb|pipe)\b")
        self.assertNotRegex(
            command_text,
            re.compile(r"(?:^|\s)(?:rb|rm)(?:\s|$)", re.MULTILINE),
        )
        for forbidden in ("--force", "--recursive", "backup.s3.bucket"):
            self.assertNotIn(
                forbidden,
                command_text,
                f"derejestracja nie moze usuwac bucketu/obiektow: {forbidden}",
            )


if __name__ == "__main__":
    unittest.main()
