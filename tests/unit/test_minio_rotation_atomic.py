#!/usr/bin/env python3
"""Rotacja poswiadczen MinIO musi byc atomowa z punktu widzenia klastra.

POWSTAL PO REALNEJ AWARII (2026-08-27, kasiopeia v8): probe `configure`
skonczone nie powodzeniem ukrylo sie za `no_log`, a przyczyne znajdowano
dopiero po pelnym logu — przeplyw zycia konta byl WTEKLE zduplikowany miedzy
kontem zapisu a kontem retencji i kazda poprawka wymagalas synchronizacji
recznej.

Dzis przeplyw istnieje w jednym egzemplarzu (reconcile_minio_account.yml),
a provision_minio.yml jedzie go dwa razy ze slownikiem `mc_account`. Ten test
strzeze niewzruszalnych wlasciwosci tego przeplywu:

  1. provisioning NIGDY nie odwoluje kont serwisowych — tylko tworzy,
     konwerguje polityke i ustala listy odwolan;
  2. swiezy create jest POTWIERDZANY sonda, zanim powstanie lista odwolan;
  3. sonda kandydata (starego klucza z secrets.env) toleruje porazke,
     sonda nowego klucza MUSI zatrzymac play;
  4. kazda faza root MinIO ma wlasny unikalny workspace i zawsze go usuwa;
  5. revoke dostaje swieze root.env dopiero PO zapisaniu secrets.env.
"""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PROVISION = REPO / "roles" / "galera_backup" / "tasks" / "provision_minio.yml"
RECONCILE = REPO / "roles" / "galera_backup" / "tasks" / "reconcile_minio_account.yml"
OWNED_KEYS = REPO / "roles" / "galera_backup" / "tasks" / "minio_owned_keys.yml"
ROOT_ENV = REPO / "roles" / "galera_backup" / "tasks" / "minio_root_env.yml"
ROLE_MAIN = REPO / "roles" / "galera_backup" / "tasks" / "main.yml"

FRAGMENT_INCLUDE = "Provision or converge scoped MinIO credentials"
FRAGMENT_DEPLOY = "Deploy cluster secrets.env"
FRAGMENT_SELECT = "Select stale MinIO service accounts"
FRAGMENT_REVOKE = "Revoke stale"
FRAGMENT_CREATE = "Create new scoped access key pair"
FRAGMENT_PROBE_NEW = "Potwierdz dostep nowego klucza MinIO"
FRAGMENT_RENDER_NEW = "Renderuj nowe poswiadczenia MinIO"
FRAGMENT_PROBE_CANDIDATE = "Verify candidate scoped credential"
FRAGMENT_CONVERGE = "Converge policy on the existing scoped credential"
FRAGMENT_REUSE_FACT = "Reuse existing scoped credentials"
FRAGMENT_TMP_CLEANUP = "Remove root credentials"
FRAGMENT_DIR_REMOVAL = "Usun katalog provisioningu MinIO"
FRAGMENT_WORKSPACE_ALLOC = "katalog tymczasowy klienta MinIO"
FRAGMENT_REVOKE_ROOT_ENV = (
    "Przygotuj swieze root-only srodowisko klienta MinIO do revoke"
)
STATIC_WORKSPACE = "/run/galera-backup-minio-tmp"
DYNAMIC_WORKSPACE = "{{ galera_backup_minio_workspace.path }}"
DYNAMIC_ROOT_ENV = f"{DYNAMIC_WORKSPACE}/root.env"




def load_tasks(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def flatten_tasks(tasks):
    """Splaszcz block/rescue/always w kolejnosci wykonywania (block, potem always)."""
    flat = []
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        flat.append(task)
        for section in ("block", "rescue", "always"):
            flat.extend(flatten_tasks(task.get(section)))
    return flat


def provision_tasks():
    return flatten_tasks(load_tasks(PROVISION))


def reconcile_tasks():
    return flatten_tasks(load_tasks(RECONCILE))


def main_tasks():
    return flatten_tasks(load_tasks(ROLE_MAIN))


def find_index(tasks, fragment):
    for index, task in enumerate(tasks):
        if fragment in str(task.get("name", "")):
            return index
    return None


def find_task(tasks, fragment):
    index = find_index(tasks, fragment)
    return tasks[index] if index is not None else None


def collect_argv(node):
    """Zbierz wszystkie listy argv z calego dokumentu YAML (rekurencyjnie)."""
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


def all_role_command_documents():
    return [load_tasks(path) for path in (PROVISION, RECONCILE, OWNED_KEYS, ROOT_ENV)]


def reconcile_includes():
    return [
        task
        for task in provision_tasks()
        if isinstance(task.get("ansible.builtin.include_tasks"), dict)
        and task["ansible.builtin.include_tasks"].get("file")
        == "reconcile_minio_account.yml"
    ]


class WorkspaceIsolation(unittest.TestCase):
    """Kazdy invocation posiada prywatny katalog z root.env i politykami."""

    def test_root_env_allocates_unique_workspace_under_run(self):
        tasks = flatten_tasks(load_tasks(ROOT_ENV))
        allocation = find_task(tasks, FRAGMENT_WORKSPACE_ALLOC)
        self.assertIsNotNone(allocation, "brak alokacji workspace MinIO")
        spec = allocation.get("ansible.builtin.tempfile", {})
        self.assertEqual(spec.get("state"), "directory")
        self.assertEqual(spec.get("path"), "/run")
        self.assertEqual(spec.get("prefix"), "galera-backup-minio-")
        self.assertEqual(
            allocation.get("register"), "galera_backup_minio_workspace"
        )
        self.assertTrue(allocation.get("become"))
        self.assertIn("groups['infra'][0]", str(allocation.get("delegate_to", "")))

    def test_all_workspace_paths_use_registered_handle(self):
        for path in (PROVISION, RECONCILE, OWNED_KEYS, ROOT_ENV, ROLE_MAIN):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(
                STATIC_WORKSPACE,
                text,
                f"{path.relative_to(REPO)} nadal wspoldzieli statyczny workspace",
            )

        root_env = find_task(
            flatten_tasks(load_tasks(ROOT_ENV)),
            "Zapisz root-only srodowisko klienta MinIO",
        )
        self.assertEqual(
            root_env["ansible.builtin.copy"].get("dest"),
            DYNAMIC_ROOT_ENV,
        )


class ProvisionNeverRevokes(unittest.TestCase):
    """Zaden plik provisioningu nie usuwa kont — revoke zyje dopiero w main.yml."""

    def test_no_accesskey_remove_anywhere_in_provisioning(self):
        offenders = [
            argv
            for document in all_role_command_documents()
            for argv in collect_argv(document)
            if "remove" in argv
        ]
        self.assertEqual(
            offenders,
            [],
            "pliki provisioningu odwoluja konta serwisowe "
            f"(argv zawiera 'remove'): {offenders}",
        )

    def test_selection_rejects_active_key_not_reuse_branch(self):
        task = find_task(reconcile_tasks(), FRAGMENT_SELECT)
        self.assertIsNotNone(task, "brak zadania wyboru kont do odwolania")
        facts = task["ansible.builtin.set_fact"]
        self.assertIn("{{ mc_account.revoke_fact }}", facts)
        expr = str(facts["{{ mc_account.revoke_fact }}"])
        # Aktywny klucz (reused albo swiezo utworzony) nigdy nie jest na liscie.
        self.assertIn("reject('equalto', lookup('vars', mc_account.access_fact))", expr)
        # Jednolity wzorzec — rozgalazienie "przy reuse odwolaj reszte, bez
        # reuse odwolaj wszystko" bylo wlasnie destrukcyjnym blisskiem.
        self.assertNotIn("galera_backup_acct_reuse", expr)


class TwoAccountsOneFlow(unittest.TestCase):
    """Dwa konta, jeden przeplyw: provision wiozi reconcile dokladnie dwa razy."""

    def test_provision_includes_reconcile_exactly_twice(self):
        includes = reconcile_includes()
        self.assertEqual(len(includes), 2, "reconcile ma miec dokladnie dwa wywolania")

    def test_write_account_uses_raw_name_and_write_facts(self):
        account = reconcile_includes()[0].get("vars", {}).get("mc_account", {})
        self.assertEqual(account.get("name"), "galera-backup-{{ cluster.name }}")
        self.assertEqual(account.get("tmp"), "")
        self.assertEqual(account.get("policy"), f"{DYNAMIC_WORKSPACE}/policy.json")
        self.assertEqual(account.get("access_fact"), "galera_backup_s3_access_key")
        self.assertEqual(account.get("secret_fact"), "galera_backup_s3_secret_key")
        self.assertEqual(
            account.get("revoke_fact"), "galera_backup_access_keys_to_revoke"
        )

    def test_retention_account_is_bounded_and_separate(self):
        account = reconcile_includes()[1].get("vars", {}).get("mc_account", {})
        name = str(account.get("name", ""))
        self.assertIn("galera-backup-prune-", name)
        self.assertIn("minio_service_account_name", name)
        self.assertEqual(account.get("tmp"), "-prune")
        self.assertEqual(
            account.get("policy"), f"{DYNAMIC_WORKSPACE}/policy-prune.json"
        )
        self.assertEqual(
            account.get("access_fact"), "galera_backup_s3_prune_access_key"
        )
        self.assertEqual(
            account.get("revoke_fact"), "galera_backup_prune_keys_to_revoke"
        )

    def test_discovery_is_shared_and_runs_before_reconciliation(self):
        discovery = [
            task
            for task in provision_tasks()
            if isinstance(task.get("ansible.builtin.include_tasks"), dict)
            and task["ansible.builtin.include_tasks"].get("file")
            == "minio_owned_keys.yml"
        ]
        self.assertEqual(len(discovery), 1, "discovery ma byc wspolny, nie kopiowany")
        tasks = provision_tasks()
        self.assertLess(
            find_index(tasks, "Zbuduj obraz stanu kont serwisowych MinIO"),
            find_index(tasks, "Pojednaj konto zapisu klastra"),
        )


class CreateIsConfirmedBeforeAnyRevoke(unittest.TestCase):
    """Create + sonda nowego klucza musi upelzniac przed lista odwolan."""

    def test_create_and_probe_precede_selection(self):
        tasks = reconcile_tasks()
        for fragment in (FRAGMENT_CREATE, FRAGMENT_PROBE_NEW, FRAGMENT_SELECT):
            self.assertIsNotNone(
                find_index(tasks, fragment), f"brak zadania '{fragment}'"
            )
        self.assertLess(find_index(tasks, FRAGMENT_CREATE), find_index(tasks, FRAGMENT_SELECT))
        self.assertLess(find_index(tasks, FRAGMENT_PROBE_NEW), find_index(tasks, FRAGMENT_SELECT))

    def test_new_credential_probe_must_fail_the_play(self):
        task = find_task(reconcile_tasks(), FRAGMENT_PROBE_NEW)
        self.assertIsNotNone(task, "brak sondy nowego poswiadczenia")
        self.assertEqual(task.get("when"), "not galera_backup_acct_reuse")
        # Sonda kandydata celowo tlumila blad (failed_when: false) — sonda
        # NOWEGO klucza nie moze: niepotwierdzony create nie moze dopuscic
        # do revoke starego konta.
        self.assertIsNone(task.get("failed_when"))
        shell = " ".join(task["ansible.builtin.command"]["argv"])
        self.assertIn("mc alias set scoped", shell)
        self.assertIn('"$GALERA_MC_AK" "$GALERA_MC_SK"', shell)

    def test_new_credential_render_gates_on_fresh_create(self):
        task = find_task(reconcile_tasks(), FRAGMENT_RENDER_NEW)
        self.assertIsNotNone(task, "brak renderu env nowego poswiadczenia")
        self.assertEqual(task.get("when"), "not galera_backup_acct_reuse")
        spec = task["ansible.builtin.copy"]
        self.assertEqual(spec.get("mode"), "0600")
        self.assertIn(
            "GALERA_MC_AK={{ lookup('vars', mc_account.access_fact) }}",
            spec.get("content", ""),
        )
        self.assertTrue(task.get("no_log"), "env z sekretem bez no_log")

    def test_create_task_still_gated_on_not_reuse(self):
        task = find_task(reconcile_tasks(), FRAGMENT_CREATE)
        self.assertIsNotNone(task, "brak zadania create")
        self.assertEqual(task.get("when"), "not galera_backup_acct_reuse")
        argv = task["ansible.builtin.command"]["argv"]
        self.assertIn("--policy", argv)
        self.assertIn("--name", argv)
        self.assertIn("{{ mc_account.name }}", argv)

    def test_provision_workspace_is_always_removed(self):
        task = find_task(provision_tasks(), FRAGMENT_TMP_CLEANUP)
        self.assertIsNotNone(task, "brak sprzatania workspace provisioningu")
        self.assertEqual(
            task["ansible.builtin.file"].get("path"),
            DYNAMIC_WORKSPACE,
        )
        when = str(task.get("when", ""))
        self.assertNotIn("access_keys_to_revoke", when)
        self.assertNotIn("prune_keys_to_revoke", when)


class RevokeRunsAfterSecretsDeploy(unittest.TestCase):
    """Revoke zyje w main.yml po zapisaniu nowego secrets.env."""

    def test_revoke_after_provision_include_and_secrets_deploy(self):
        tasks = main_tasks()
        i_include = find_index(tasks, FRAGMENT_INCLUDE)
        i_deploy = find_index(tasks, FRAGMENT_DEPLOY)
        i_revoke = find_index(tasks, FRAGMENT_REVOKE)
        self.assertIsNotNone(i_include, "brak include provision_minio.yml")
        self.assertIsNotNone(i_deploy, "brak deploy secrets.env")
        self.assertIsNotNone(i_revoke, "brak zadania revoke w main.yml")
        self.assertLess(i_include, i_deploy)
        self.assertLess(i_deploy, i_revoke)

    def test_revoke_allocates_fresh_workspace_after_secrets_deploy(self):
        tasks = main_tasks()
        i_deploy = find_index(tasks, FRAGMENT_DEPLOY)
        i_root_env = find_index(tasks, FRAGMENT_REVOKE_ROOT_ENV)
        i_revoke = find_index(tasks, FRAGMENT_REVOKE)
        self.assertIsNotNone(i_root_env, "revoke nie tworzy swiezego root.env")
        root_env = tasks[i_root_env]
        self.assertEqual(
            root_env["ansible.builtin.include_tasks"].get("file"),
            "minio_root_env.yml",
        )
        self.assertLess(i_deploy, i_root_env)
        self.assertLess(i_root_env, i_revoke)

    def test_revoke_fails_closed_when_workspace_is_missing(self):
        tasks = [
            task
            for task in main_tasks()
            if "Revoke stale MinIO" in str(task.get("name", ""))
        ]
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            when = " ".join(task.get("when", []))
            # Pusta lista odwolan wycina zadanie PRZED templatowaniem argv,
            # wiec no-op nigdy nie dotyka `.path` niezdefiniowanego workspace.
            self.assertIn("default([])", str(task.get("loop", "")))
            self.assertIn("length) > 0", when)
            # Brak workspace przy NIEPUSTEJ liscie to blad: play ma paść na
            # niezdefiniowanej zmiennej, a nie po cichu zostawic zywe konta
            # serwisowe z waznym sekretem do bucketa kopii.
            self.assertNotIn("galera_backup_minio_workspace is defined", when)
            self.assertNotIn("galera_backup_minio_workspace.path is defined", when)


    def test_revoke_delegated_gated_and_silent(self):
        task = find_task(main_tasks(), FRAGMENT_REVOKE)
        self.assertIsNotNone(task)
        self.assertIn("groups['infra']", str(task.get("delegate_to", "")))
        when = task.get("when")
        if isinstance(when, str):
            when = [when]
        joined = " ".join(when or [])
        self.assertIn("galera_backup_managed_minio", joined)
        self.assertIn("backup.scheduler.host", joined)
        self.assertTrue(task.get("no_log"))
        self.assertEqual(task.get("changed_when"), True)

    def test_revoke_workspace_removed_after_revoke(self):
        tasks = main_tasks()
        i_revoke = find_index(tasks, FRAGMENT_REVOKE)
        i_removal = find_index(tasks, FRAGMENT_DIR_REMOVAL)
        self.assertIsNotNone(i_removal, "brak sprzatania workspace po revoke")
        self.assertIsNotNone(i_revoke)
        self.assertLess(i_revoke, i_removal)
        removal = tasks[i_removal]
        self.assertEqual(removal["ansible.builtin.file"]["state"], "absent")
        self.assertEqual(
            removal["ansible.builtin.file"]["path"],
            DYNAMIC_WORKSPACE,
        )
        self.assertIn("groups['infra']", str(removal.get("delegate_to", "")))
        self.assertTrue(removal.get("become"))
        # Jedyne zadanie kasujace root.env ze wspoldzielonego hosta infra nie
        # moze zalezec od recznej kopii warunku include'u tworzacego workspace:
        # dryf miedzy tymi listami zostawia poswiadczenia root na infra.
        when = " ".join(removal.get("when", []))
        self.assertNotIn("access_keys_to_revoke", when)
        self.assertNotIn("prune_keys_to_revoke", when)

    def test_provision_include_pins_infra_delegation(self):
        include = find_task(main_tasks(), FRAGMENT_INCLUDE)
        self.assertIsNotNone(include, "brak include provision_minio.yml")
        spec = include["ansible.builtin.include_tasks"]
        # Zadania provisioningu — w tym cleanup root.env — nie maja wlasnego
        # delegate_to i biegna na infra WYLACZNIE dzieki temu apply.
        self.assertIn(
            "groups['infra'][0]", str(spec.get("apply", {}).get("delegate_to", ""))
        )

    def test_every_workspace_cleanup_runs_as_root(self):
        cleanups = [
            find_task(provision_tasks(), FRAGMENT_TMP_CLEANUP),
            find_task(main_tasks(), FRAGMENT_DIR_REMOVAL),
        ]
        for cleanup in cleanups:
            self.assertIsNotNone(cleanup)
            self.assertTrue(cleanup.get("become"), cleanup.get("name"))


class ReusePathUntouched(unittest.TestCase):
    """Sciezka reuse musi zachowac dotychczasowe zachowanie."""

    def test_candidate_probe_still_tolerates_failure(self):
        task = find_task(reconcile_tasks(), FRAGMENT_PROBE_CANDIDATE)
        self.assertIsNotNone(task, "brak sondy kandydata")
        self.assertEqual(task.get("failed_when"), False)

    def test_converge_policy_unchanged(self):
        task = find_task(reconcile_tasks(), FRAGMENT_CONVERGE)
        self.assertIsNotNone(task, "brak konwergencji polityki")
        self.assertEqual(task.get("when"), "galera_backup_acct_reuse")
        argv = task["ansible.builtin.command"]["argv"]
        self.assertIn("edit", argv)
        self.assertIn("--policy", argv)

    def test_reuse_fact_routes_into_caller_owned_names(self):
        task = find_task(reconcile_tasks(), FRAGMENT_REUSE_FACT)
        self.assertIsNotNone(task, "brak zadania reuse")
        self.assertEqual(task.get("when"), "galera_backup_acct_reuse")
        facts = task["ansible.builtin.set_fact"]
        # Klucze faktow sa dynamiczne (nazwa przekazana przez mc_account), wiec
        # w YAML widac placeholdery — wartoscia jest poswiadczenie z secrets.env.
        self.assertIn("{{ mc_account.access_fact }}", facts)
        self.assertEqual(
            facts["{{ mc_account.access_fact }}"],
            "{{ mc_account.existing_access_key }}",
        )


if __name__ == "__main__":
    unittest.main()
