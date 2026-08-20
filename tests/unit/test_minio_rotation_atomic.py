#!/usr/bin/env python3
"""Rotacja poswiadczen MinIO musi byc atomowa z punktu widzenia klastra.

POWSTAL PO BLISKAJACEJ AWARII (n14/n15, 2026-08-19): dotychczasowy porzadek
zadan w provision_minio.yml biegł "Revoke stale service accounts" PRZED
"Create new scoped access key pair", a sciezka bez reuse odwolywala WSZYSTKIE
istniejace konta. Gdy create padal — albo gdy pozniejszy zapis secrets.env
padal — stare konto juz nie istnialo, a schedulery i hosty restore trzymaly w
secrets.env wlasnie odwolany klucz. Cronowy backup i restore drill dostawaly
SignatureDoesNotMatch az do kolejnego udanego configure: kopia byla
niedostepona przy zywych, zdrowych komponentach.

Kontrakt atomowosci rotacji pilnowany przez ten plik:
  1. provision_minio.yml NIGDY nie odwoluje kont serwisowych — tylko tworzy,
     konwerguje polityke i sonduje. Samo revoke zyje w main.yml.
  2. Revoke biegnie dopiero PO "Deploy cluster secrets.env": porazka create
     albo zapisu sekretow zatrzymuje play ZANIM stare konto zniknie.
  3. Liste odwolan wylicza sie wzgledem AKTYWNEGO klucza
     (galera_backup_s3_access_key) — wspolnie dla reuse i swiezego klucza,
     nigdy "wszystko, co istnieje".
  4. Swiezo utworzone poswiadczenie jest sondowane (mc alias set + ls) zanim
     jakiekolwiek konto zostanie odwolane — "potwierdzony create".
  5. Sciezka reuse pozostaje nietknieta: sonda kandydata nadal tlumi blad
     (failed_when: false), konwergencja polityki i reuse dzialaja po staremu.
"""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PROVISION = REPO / "roles" / "galera_backup" / "tasks" / "provision_minio.yml"
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
        for section in ("block", "rescue", "always"):
            flat.extend(flatten_tasks(task.get(section)))
        if "block" not in task:
            flat.append(task)
    return flat


def provision_tasks():
    return flatten_tasks(load_tasks(PROVISION))


def main_tasks():
    return flatten_tasks(load_tasks(ROLE_MAIN))


def find_index(tasks, fragment):
    for index, task in enumerate(tasks):
        if fragment in task.get("name", ""):
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
        for item in node:
            found.extend(collect_argv(item))
    return found


class ProvisionNeverRevokes(unittest.TestCase):
    """Plik provisioningu nie moze usuwac kont — revoke zyje dopiero w main.yml."""

    def test_no_accesskey_remove_in_provision(self):
        argvs = collect_argv(load_tasks(PROVISION))
        offenders = [argv for argv in argvs if "remove" in argv]
        self.assertEqual(
            offenders,
            [],
            "provision_minio.yml odwoluje konta serwisowe "
            f"(argv zawiera 'remove'): {offenders}",
        )

    def test_selection_rejects_active_key_not_reuse_branch(self):
        task = find_task(provision_tasks(), FRAGMENT_SELECT)
        self.assertIsNotNone(task, "brak zadania wyboru kont do odwolania")
        expr = str(
            task["ansible.builtin.set_fact"]["galera_backup_access_keys_to_revoke"]
        )
        # Aktywny klucz (reused albo swiezo utworzony) nigdy nie jest na liscie.
        self.assertIn("reject('equalto', galera_backup_s3_access_key)", expr)
        # Jednolity wzorzec — rozgalazienie "przy reuse odwolaj reszte, bez
        # reuse odwolaj wszystko" bylo wlasnie destrukcyjnym blisskiem.
        self.assertNotIn("galera_backup_reuse_existing_s3_key", expr)


class CreateIsConfirmedBeforeAnyRevoke(unittest.TestCase):
    """Create + sonda nowego klucza musi upelzniac przed lista odwolan."""

    def test_create_and_probe_precede_selection(self):
        tasks = provision_tasks()
        for fragment in (FRAGMENT_CREATE, FRAGMENT_PROBE_NEW, FRAGMENT_SELECT):
            self.assertIsNotNone(
                find_index(tasks, fragment), f"brak zadania '{fragment}'"
            )
        self.assertLess(find_index(tasks, FRAGMENT_CREATE), find_index(tasks, FRAGMENT_SELECT))
        self.assertLess(find_index(tasks, FRAGMENT_PROBE_NEW), find_index(tasks, FRAGMENT_SELECT))

    def test_new_credential_probe_must_fail_the_play(self):
        task = find_task(provision_tasks(), FRAGMENT_PROBE_NEW)
        self.assertIsNotNone(task, "brak sondy nowego poswiadczenia")
        self.assertEqual(task.get("when"), "not galera_backup_reuse_existing_s3_key")
        # Sonda kandydata celowo tlumila blad (failed_when: false) — sonda
        # NOWEGO klucza nie moze: niepotwierdzony create nie może dopuscic
        # do revoke starego konta.
        self.assertIsNone(task.get("failed_when"))
        shell = " ".join(task["ansible.builtin.command"]["argv"])
        self.assertIn("mc alias set scoped", shell)
        self.assertIn('"$GALERA_MC_AK" "$GALERA_MC_SK"', shell)

    def test_new_credential_render_gates_on_fresh_create(self):
        task = find_task(provision_tasks(), FRAGMENT_RENDER_NEW)
        self.assertIsNotNone(task, "brak renderu env nowego poswiadczenia")
        self.assertEqual(task.get("when"), "not galera_backup_reuse_existing_s3_key")
        spec = task["ansible.builtin.copy"]
        self.assertEqual(spec.get("mode"), "0600")
        self.assertIn("GALERA_MC_AK={{ galera_backup_s3_access_key }}", spec.get("content", ""))
        self.assertTrue(task.get("no_log"), "env z sekretem bez no_log")

    def test_create_task_still_gated_on_not_reuse(self):
        task = find_task(provision_tasks(), FRAGMENT_CREATE)
        self.assertIsNotNone(task, "brak zadania create")
        self.assertEqual(task.get("when"), "not galera_backup_reuse_existing_s3_key")
        argv = task["ansible.builtin.command"]["argv"]
        self.assertIn("--policy", argv)
        self.assertIn("--name", argv)

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
        task = find_task(provision_tasks(), FRAGMENT_PROBE_CANDIDATE)
        self.assertIsNotNone(task, "brak sondy kandydata")
        self.assertEqual(task.get("failed_when"), False)

    def test_converge_policy_unchanged(self):
        task = find_task(provision_tasks(), FRAGMENT_CONVERGE)
        self.assertIsNotNone(task, "brak konwergencji polityki")
        self.assertEqual(task.get("when"), "galera_backup_reuse_existing_s3_key")
        argv = task["ansible.builtin.command"]["argv"]
        self.assertIn("edit", argv)
        self.assertIn("--policy", argv)

    def test_reuse_fact_unchanged(self):
        task = find_task(provision_tasks(), FRAGMENT_REUSE_FACT)
        self.assertIsNotNone(task, "brak zadania reuse")
        self.assertEqual(task.get("when"), "galera_backup_reuse_existing_s3_key")
        facts = task["ansible.builtin.set_fact"]
        self.assertEqual(
            facts["galera_backup_s3_access_key"],
            "{{ galera_backup_existing_s3_access_key }}",
        )


if __name__ == "__main__":
    unittest.main()
