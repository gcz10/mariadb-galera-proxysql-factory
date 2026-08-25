"""Kontrakty lifecycle obrazu i backupow PMM warstwy wspolnej."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOK = REPO / "playbooks" / "infra_services.yml"


def load_tasks(tasks):
    """Rozwin statyczne include_tasks, zachowujac kolejnosc wykonania."""
    loaded = []
    for task in tasks:
        include = task.get("ansible.builtin.include_tasks")
        if include is None:
            loaded.append(task)
            continue
        include_path = include["file"] if isinstance(include, dict) else include
        nested = yaml.safe_load((PLAYBOOK.parent / include_path).read_text(encoding="utf-8"))
        loaded.extend(load_tasks(nested))
    return loaded


class PlatformPmmUpgradeSafetyContractTests(unittest.TestCase):
    """Zmiana obrazu PMM nie moze wyprzedzic zweryfikowanego backupu /srv."""

    @classmethod
    def setUpClass(cls):
        cls.text = PLAYBOOK.read_text(encoding="utf-8")
        cls.plays = yaml.safe_load(cls.text)
        # Play wyszukujemy po TRESCI, nie po tytule: tytul jest opisem dla
        # operatora i zmienil sie przy rozdzielaniu uslug warstwy wspolnej,
        # przez co kontrakt przestal cokolwiek sprawdzac (StopIteration).
        play = next(
            item
            for item in cls.plays
            if any(
                "infra_pmm_upgrade.yml" in str(task.get("ansible.builtin.include_tasks", ""))
                for task in (item.get("tasks") or [])
            )
        )
        cls.play = play
        cls.play_vars = play["vars"]
        cls.tasks = load_tasks(play["tasks"])
        cls.by_name = {task.get("name"): task for task in cls.tasks}
        cls.names = [task.get("name") for task in cls.tasks]

    def test_platform_ownership_guard_precedes_firewall(self):
        names = [play.get("name") for play in self.plays]
        guard_name = "Infra — wymagaj ownership warstwy wspólnej"
        firewall_name = "Infra — wymuś dokładną politykę host firewall przed uruchomieniem usług"
        self.assertLess(names.index(guard_name), names.index(firewall_name))

        guard = next(play for play in self.plays if play.get("name") == guard_name)
        self.assertEqual(guard["hosts"], "infra")
        self.assertTrue(guard["any_errors_fatal"])
        self.assertFalse(guard["gather_facts"])
        task = guard["tasks"][0]["ansible.builtin.assert"]
        conditions = "\\n".join(task["that"])
        self.assertIn("platform is defined", conditions)
        self.assertIn("cluster is not defined", conditions)

    @unittest.skipIf(shutil.which("make") is None, "make niedostepny")
    def test_tenant_cluster_infra_entrypoint_is_removed(self):
        proc = subprocess.run(
            ["make", "-n", "cluster-infra"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("No rule to make target", proc.stderr)

    def test_backup_gate_precedes_compose_recreation(self):
        backup_gate = "PMM — wymagaj kompletnego backupu przed zmianą obrazu"
        compose_up = "Uruchom usługi infra"
        self.assertIn(backup_gate, self.names)
        self.assertLess(
            self.names.index(backup_gate),
            self.names.index(compose_up),
            "backup PMM musi zostac zweryfikowany przed docker compose up",
        )

    def test_container_and_volume_state_matrix_fails_closed(self):
        containers = self.by_name["PMM — znajdź kontener"]
        container_argv = containers["ansible.builtin.command"]["argv"]
        self.assertEqual(
            container_argv[:5],
            ["docker", "container", "ls", "--all", "--format"],
        )
        self.assertIn(".Names", container_argv[5])
        self.assertEqual(container_argv[6:], ["--filter", "name=pmm-server"])
        self.assertNotIn("failed_when", containers)

        volumes = self.by_name["PMM — znajdź volume danych"]
        volume_argv = volumes["ansible.builtin.command"]["argv"]
        self.assertEqual(
            volume_argv[:4],
            ["docker", "volume", "ls", "--format"],
        )
        self.assertIn(".Name", volume_argv[4])
        self.assertEqual(
            volume_argv[5:],
            ["--filter", "name={{ infra_pmm_data_volume }}"],
        )
        self.assertNotIn("failed_when", volumes)

        classify = self.by_name["PMM — sklasyfikuj stan danych"]
        expression = str(classify["ansible.builtin.set_fact"]["infra_pmm_state"])
        template = jinja2.Template(expression)

        def state(container_exists, volume_exists):
            return template.render(
                infra_pmm_container_list={
                    "stdout_lines": ["pmm-server"] if container_exists else []
                },
                infra_pmm_volume_list={
                    "stdout_lines": ["isa-pmm-data"] if volume_exists else []
                },
                infra_pmm_data_volume="isa-pmm-data",
            ).strip()

        self.assertEqual(state(False, False), "fresh")
        self.assertEqual(state(True, True), "managed")
        self.assertEqual(state(False, True), "orphaned-data")
        self.assertEqual(state(True, False), "missing-data")

        guard = self.by_name["PMM — wymagaj spójnego stanu kontenera i danych"]
        self.assertEqual(
            guard["ansible.builtin.assert"]["that"],
            ["infra_pmm_state in ['fresh', 'managed']"],
        )

    def test_backup_runs_only_for_managed_container_with_changed_image(self):
        inspect = self.by_name["PMM — odczytaj bieżący obraz kontenera"]
        inspect_argv = inspect["ansible.builtin.command"]["argv"]
        self.assertEqual(inspect_argv[:3], ["docker", "container", "inspect"])
        self.assertIn("--format", inspect_argv)
        self.assertTrue(
            any(".Config.Image" in str(argument) for argument in inspect_argv),
            "inspect ma zwracac tylko obraz, nigdy Config.Env z haslem PMM",
        )
        self.assertIn("infra_pmm_state == 'managed'", str(inspect["when"]))

        facts = self.by_name["PMM — wylicz potrzebę backupu przed zmianą obrazu"]
        expression = str(
            facts["ansible.builtin.set_fact"]["infra_pmm_upgrade_required"]
        )
        predicate = jinja2.Template(expression)

        def upgrade_required(state, current, desired):
            rendered = predicate.render(
                infra_pmm_state=state,
                infra_pmm_current_image=current,
                infra_pmm_desired_image=desired,
            )
            return rendered.strip().lower() == "true"

        desired = "percona/pmm-server:3.9.1@sha256:target"
        self.assertFalse(upgrade_required("fresh", "", desired))
        self.assertFalse(upgrade_required("managed", desired, desired))
        self.assertTrue(
            upgrade_required(
                "managed",
                "percona/pmm-server:3.9.0@sha256:old",
                desired,
            ),
            "zarzadzany kontener z innym obrazem wymaga backupu",
        )

    def test_backup_is_read_only_at_source_verified_and_restart_safe(self):
        backup = self.by_name["PMM — wykonaj spójny backup przed zmianą obrazu"]
        conditions = "\n".join(backup["when"])
        self.assertIn("infra_pmm_upgrade_required", conditions)
        self.assertNotIn("infra_pmm_backup_inspect", conditions)

        block = {task["name"]: task for task in backup["block"]}
        copy_argv = block["PMM — skopiuj i zweryfikuj dane /srv"][
            "ansible.builtin.command"
        ]["argv"]
        self.assertIn("{{ infra_pmm_data_volume }}:/from:ro", copy_argv)
        self.assertIn("{{ infra_pmm_backup_volume }}:/to", copy_argv)
        copy_script = copy_argv[-1]
        self.assertIn("test -d /to/grafana", copy_script)
        self.assertIn("test -d /to/victoriametrics", copy_script)
        self.assertIn(".isa-pmm-backup-complete", copy_script)
        restart = {task["name"]: task for task in backup["always"]}[
            "PMM — uruchom serwer po backupie"
        ]
        self.assertEqual(
            restart["ansible.builtin.command"]["argv"],
            ["docker", "start", "pmm-server"],
        )

    def test_backup_volume_is_labelled_and_sorts_newest_first(self):
        volume = str(self.play_vars["infra_pmm_backup_volume"])
        self.assertLess(
            volume.index("ansible_date_time.epoch"),
            volume.index("lock.pmm.version"),
        )
        backup = self.by_name["PMM — wykonaj spójny backup przed zmianą obrazu"]
        block = {task["name"]: task for task in backup["block"]}
        create_argv = block["PMM — utwórz volume backupu"][
            "ansible.builtin.command"
        ]["argv"]
        self.assertIn("isa.pmm.backup=true", create_argv)
        self.assertEqual(create_argv[-1], "{{ infra_pmm_backup_volume }}")

    def test_backup_retention_is_bounded_config_and_prunes_only_verified_labels(self):
        schema = yaml.safe_load(
            (REPO / "platform" / "schema" / "platform.schema.json").read_text(
                encoding="utf-8"
            )
        )
        pmm_schema = schema["properties"]["monitoring"]["properties"]["pmm"]
        self.assertIn("backup_retention", pmm_schema["required"])
        retention_schema = pmm_schema["properties"]["backup_retention"]
        self.assertEqual(retention_schema["minimum"], 1)
        self.assertEqual(retention_schema["maximum"], 5)


        config_guard = next(
            task
            for task in self.play["pre_tasks"]
            if task.get("name") == "Wymagaj kompletnej konfiguracji infra"
        )
        conditions = "\n".join(config_guard["ansible.builtin.assert"]["that"])
        self.assertIn("monitoring.pmm.backup_retention", conditions)
        self.assertNotIn("no_log", config_guard)
        # Sekret jest wymagany WTEDY, gdy platforma deklaruje usluge, ktora go
        # uzywa. Twarde zadanie MINIO_* blokowalo warstwe bez magazynu kopii —
        # konfiguracje legalna, bo S3 bywa usluga zewnetrzna.
        secret_guard = next(
            task
            for task in self.play["pre_tasks"]
            if str(task.get("name", "")).startswith("Wymagaj sekret")
        )
        self.assertTrue(secret_guard["no_log"])
        conditions = secret_guard["ansible.builtin.assert"]["that"]
        self.assertEqual(
            conditions,
            [
                "'pmm' not in infra_services or pmm_admin_password | length >= 12",
                "'minio' not in infra_services or minio_root_user | length >= 3",
                "'minio' not in infra_services or minio_root_password | length >= 12",
            ],
        )
        platform = yaml.safe_load(
            (REPO / "platform" / "shared" / "platform.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(platform["monitoring"]["pmm"]["backup_retention"], 2)

        list_task = self.by_name["PMM — znajdź zarządzane backupy"]
        list_argv = list_task["ansible.builtin.command"]["argv"]
        self.assertEqual(list_argv[:3], ["docker", "volume", "ls"])
        self.assertIn("label=isa.pmm.backup=true", list_argv)
        self.assertIn(
            "label=isa.pmm.source={{ infra_pmm_data_volume }}",
            list_argv,
        )

        verify = self.by_name["PMM — zweryfikuj zarządzane backupy"]
        verify_argv = verify["ansible.builtin.command"]["argv"]
        self.assertIn("{{ item }}:/backup:ro", verify_argv)
        self.assertIn(".isa-pmm-backup-complete", verify_argv[-1])
        self.assertIn("test -d /backup/grafana", verify_argv[-1])
        self.assertIn("test -d /backup/victoriametrics", verify_argv[-1])
        self.assertEqual(verify["register"], "infra_pmm_backup_probe")
        self.assertFalse(verify["failed_when"])

        reset = self.by_name["PMM — wyzeruj listę backupów do zachowania"]
        self.assertEqual(
            reset["ansible.builtin.set_fact"]["infra_pmm_backup_keep"],
            [],
        )

        select = self.by_name["PMM — wybierz zweryfikowane backupy do zachowania"]
        probes = [
            {"item": "isa-pmm-data-backup-400-bad", "rc": 1},
            {"item": "isa-pmm-data-backup-300-good", "rc": 0},
            {"item": "isa-pmm-data-backup-200-good", "rc": 0},
            {"item": "isa-pmm-data-backup-100-good", "rc": 0},
        ]
        rendered_loop = jinja2.Template(str(select["loop"])).render(
            infra_pmm_backup_probe={"results": probes}
        )
        ordered_probes = yaml.safe_load(rendered_loop)
        keep = []
        for item in ordered_probes:
            context = {
                "item": item,
                "infra_pmm_backup_keep": keep,
                "monitoring": {"pmm": {"backup_retention": 2}},
            }
            selected = all(
                jinja2.Template("{{ " + condition + " }}")
                .render(**context)
                .strip()
                .lower()
                == "true"
                for condition in select["when"]
            )
            if selected:
                rendered_keep = jinja2.Template(
                    str(
                        select["ansible.builtin.set_fact"][
                            "infra_pmm_backup_keep"
                        ]
                    )
                ).render(**context)
                keep = yaml.safe_load(rendered_keep)
        self.assertEqual(
            keep,
            [
                "isa-pmm-data-backup-300-good",
                "isa-pmm-data-backup-200-good",
            ],
            "nowszy uszkodzony backup nie moze wyprzec starszej dobrej generacji",
        )

        remove = self.by_name["PMM — usuń backupy nieweryfikowalne i ponad retencję"]
        self.assertEqual(
            remove["ansible.builtin.command"]["argv"],
            ["docker", "volume", "rm", "{{ item }}"],
        )
        rendered_remove_loop = jinja2.Template(str(remove["loop"])).render(
            infra_pmm_backup_list={
                "stdout_lines": [probe["item"] for probe in probes]
            }
        )
        remove_candidates = yaml.safe_load(rendered_remove_loop)
        remove_predicate = jinja2.Template(
            "{{ " + str(remove["when"]) + " }}"
        )
        removed = [
            item
            for item in remove_candidates
            if remove_predicate.render(
                item=item,
                infra_pmm_backup_keep=keep,
            ).strip().lower()
            == "true"
        ]
        self.assertEqual(
            removed,
            [
                "isa-pmm-data-backup-400-bad",
                "isa-pmm-data-backup-100-good",
            ],
        )

        upgrade_guard = self.by_name[
            "PMM — wymagaj bieżącego zweryfikowanego backupu po zmianie obrazu"
        ]
        self.assertEqual(
            upgrade_guard["ansible.builtin.assert"]["that"],
            ["infra_pmm_backup_volume in infra_pmm_backup_keep"],
        )
        self.assertIn("infra_pmm_upgrade_required", str(upgrade_guard["when"]))
        self.assertGreater(
            self.names.index("PMM — znajdź zarządzane backupy"),
            self.names.index(
                "PMM — zmien domyslne haslo admina przez Grafana User API (uri, bez wycieku w argv)"
            ),
            "retencja moze ruszyc dopiero po gotowosci i rotacji hasla PMM",
        )

    def test_retention_fails_closed_when_every_probe_fails(self):
        guard_name = "PMM — wymagaj sprawnej weryfikacji przed usuwaniem"
        guard = self.by_name[guard_name]
        self.assertEqual(
            guard["ansible.builtin.assert"]["that"],
            ["infra_pmm_backup_keep | length > 0"],
        )
        should_run = jinja2.Template("{{ " + str(guard["when"]) + " }}").render(
            infra_pmm_backup_list={"stdout_lines": ["bad-new", "bad-old"]}
        )
        self.assertEqual(should_run.strip().lower(), "true")
        fresh_host = jinja2.Template(
            "{{ " + str(guard["when"]) + " }}"
        ).render(infra_pmm_backup_list={"stdout_lines": []})
        self.assertEqual(fresh_host.strip().lower(), "false")
        assertion = jinja2.Template(
            "{{ " + guard["ansible.builtin.assert"]["that"][0] + " }}"
        ).render(infra_pmm_backup_keep=[])
        self.assertEqual(assertion.strip().lower(), "false")

        remove_name = "PMM — usuń backupy nieweryfikowalne i ponad retencję"
        remove = self.by_name[remove_name]
        self.assertLess(self.names.index(guard_name), self.names.index(remove_name))
        self.assertNotIn("sort", str(remove["loop"]))

    def test_failed_backup_cleans_only_attempt_volume_and_aborts(self):
        backup = self.by_name["PMM — wykonaj spójny backup przed zmianą obrazu"]
        rescue = {task["name"]: task for task in backup["rescue"]}
        cleanup = rescue["PMM — usuń niekompletny volume backupu"]
        cleanup_argv = cleanup["ansible.builtin.command"]["argv"]
        self.assertEqual(
            cleanup_argv,
            ["docker", "volume", "rm", "--force", "{{ infra_pmm_backup_volume }}"],
        )
        self.assertNotIn("{{ infra_pmm_data_volume }}", cleanup_argv)
        probe = rescue["PMM — sprawdź stan volume po cleanupie"]
        probe_argv = probe["ansible.builtin.command"]["argv"]
        self.assertEqual(probe_argv[:4], ["docker", "volume", "ls", "--format"])
        self.assertIn(".Name", probe_argv[4])
        self.assertEqual(
            probe_argv[5:],
            ["--filter", "name={{ infra_pmm_backup_volume }}"],
        )
        self.assertFalse(probe["failed_when"])
        self.assertIn("PMM — przerwij po nieudanym backupie", rescue)

        failure = rescue["PMM — przerwij po nieudanym backupie"]
        message = jinja2.Template(str(failure["ansible.builtin.fail"]["msg"]))

        absent = message.render(
            infra_pmm_backup_volume="backup-attempt",
            ansible_failed_result={"stderr": "copy verification failed"},
            infra_pmm_backup_cleanup={"rc": 1, "stderr": "no such volume"},
            infra_pmm_backup_after_cleanup={"rc": 0, "stdout_lines": [], "stderr": ""},
        )
        self.assertIn("copy verification failed", absent)
        self.assertIn("nie istnieje", absent)
        self.assertNotIn("pozostal", absent)

        remained = message.render(
            infra_pmm_backup_volume="backup-attempt",
            ansible_failed_result={"stderr": "copy verification failed"},
            infra_pmm_backup_cleanup={"rc": 1, "stderr": "volume is in use"},
            infra_pmm_backup_after_cleanup={
                "rc": 0,
                "stdout_lines": ["backup-attempt"],
                "stderr": "",
            },
        )
        self.assertIn("copy verification failed", remained)
        self.assertIn("backup-attempt", remained)
        self.assertIn("volume is in use", remained)
        self.assertIn("pozostal", remained)

        unknown = message.render(
            infra_pmm_backup_volume="backup-attempt",
            ansible_failed_result={"stderr": "copy verification failed"},
            infra_pmm_backup_cleanup={"rc": 1, "stderr": "daemon unavailable"},
            infra_pmm_backup_after_cleanup={
                "rc": 1,
                "stdout_lines": [],
                "stderr": "cannot inspect volumes",
            },
        )
        self.assertIn("stan volume po cleanupie jest nieznany", unknown.lower())
        self.assertIn("cannot inspect volumes", unknown)

    def backup_gate_script(self, backup_dir):
        gate = self.by_name["PMM — wymagaj kompletnego backupu przed zmianą obrazu"]
        script = gate["ansible.builtin.command"]["argv"][-1]
        return script.replace("/backup", str(backup_dir))

    def test_backup_gate_rejects_missing_completion_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp)
            (backup / "grafana").mkdir()
            (backup / "victoriametrics").mkdir()
            proc = subprocess.run(
                ["sh", "-ceu", self.backup_gate_script(backup)],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(proc.returncode, 0, "niekompletny backup musi blokowac upgrade")

    def test_backup_gate_accepts_verified_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp)
            (backup / "grafana").mkdir()
            (backup / "victoriametrics").mkdir()
            (backup / ".isa-pmm-backup-complete").write_text(
                "source -> target\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                ["sh", "-ceu", self.backup_gate_script(backup)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
