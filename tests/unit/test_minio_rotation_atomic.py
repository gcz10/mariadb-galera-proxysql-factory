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
  4. katalog z poswiadczeniami root zyje do momentu odwolan w main.yml;
  5. w main.yml revoke biegnie dopiero PO zapisaniu secrets.env.
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
        self.assertEqual(account.get("policy"), "/run/galera-backup-minio-tmp/policy.json")
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
            account.get("policy"), "/run/galera-backup-minio-tmp/policy-prune.json"
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

    def test_tmp_dir_survives_while_revocation_pending(self):
        task = find_task(provision_tasks(), FRAGMENT_TMP_CLEANUP)
        self.assertIsNotNone(task, "brak sprzatania katalogu tymczasowego")
        when = str(task.get("when", ""))
        # root.env musi przezyc do revoke w main.yml — sprzatamy katalog tylko
        # gdy nic nie czeka na odwolanie.
        self.assertIn("default([])", when)
        self.assertIn("== 0", when)


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

    def test_revoke_loop_defaults_to_empty(self):
        task = find_task(main_tasks(), FRAGMENT_REVOKE)
        self.assertIsNotNone(task)
        loop = str(task.get("loop", ""))
        # Na hostach bez zarzadanego MinIO fakt nigdy nie powstaje — petla
        # bez default([]) wywalalaby play bledem szablonu mimo when=false.
        self.assertIn("default([])", loop)

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

    def test_provision_dir_removed_after_revoke(self):
        tasks = main_tasks()
        i_revoke = find_index(tasks, FRAGMENT_REVOKE)
        i_removal = find_index(tasks, FRAGMENT_DIR_REMOVAL)
        self.assertIsNotNone(i_removal, "brak sprzatania katalogu po revoke")
        self.assertIsNotNone(i_revoke)
        self.assertLess(i_revoke, i_removal)
        removal = tasks[i_removal]
        self.assertEqual(removal["ansible.builtin.file"]["state"], "absent")
        self.assertIn("groups['infra']", str(removal.get("delegate_to", "")))


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
