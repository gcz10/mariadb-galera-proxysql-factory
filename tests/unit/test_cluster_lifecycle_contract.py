"""Testy kontraktu lifecycle klastra: cluster-build / cluster-recover.

Kontrakt sprawdzany na Makefile i playbooks/cluster_recover.yml:

1. cluster-build to jedna, jawna orkiestracja ISTNIEJACYCH celow, w kolejnosci
   zaleznosci, zakonczona bramka lab-post-build-gate; CLUSTER/CONFIRM
   propaguja sie na pod-make, a pierwszy blad konczy caly build.
2. cluster-deploy juz zawiera firewall — cluster-build NIE doklada drugiego
   kroku firewall.
3. cluster-recover jest confirm-gated: wymaga CLUSTER+CONFIRM=yes, odmawia
   pracy przy zywym Primary, wybiera wezel bootstrap JAWNIE
   (safe_to_bootstrap=1 albo unikalny najwyzszy seqno; przy remisie wymaga
   BOOTSTRAP_NODE) i reuse'uje kanoniczny playbooks/bootstrap.yml przez
   parametry (bootstrap_node, bootstrap_confirm_all_down=true), potem join.
4. Zadna sciezka recovery nie robi rownoleglego `systemctl restart`.
"""

import base64
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import jinja2
import yaml

REPO = Path(__file__).resolve().parents[2]
JOIN_PLAYBOOK = REPO / "playbooks" / "f5_join.yml"

# `cluster-endpoint` znikl z tej listy 2026-08-21 wraz z wyniesieniem warstwy
# wspolnej: VIP nalezy do `platform/shared/`, a nie do najemcy. Zostawienie go
# tutaj bylo bledem projektowym — build KAZDEGO klastra dotykal Keepalived na
# wspoldzielonej parze fcp1/fcp2, bez zadnej bramki wlasciciela.
CORE_BUILD_STEPS = [
    "cluster-validate",
    "cluster-deploy",
    "cluster-bootstrap",
    "cluster-join",
    "cluster-proxysql",
    "cluster-monitoring",
    "cluster-harden",
]
CONDITIONAL_BUILD_STEPS = [
    "lab-seed-smoke",
    "cluster-alerts",
    "cluster-app-host",
]
BACKUP_BUILD_STEPS = [
    "cluster-backup-configure",
    "cluster-backup",
    "cluster-restore-drill",
    "cluster-monitoring-refresh",
]
BUILD_GATE = "lab-post-build-gate"


def makefile_text():
    return (REPO / "Makefile").read_text(encoding="utf-8")


def phony_blob(text):
    """Calosc deklaracji .PHONY (wielolinijkowej, z kontynuacjami \\)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(".PHONY:"):
            blob = []
            current = line
            while True:
                blob.append(current.rstrip("\\").strip())
                if not current.endswith("\\"):
                    break
                current = lines[index + len(blob)]
            return " ".join(blob)
    return None


def parse_makefile(text):
    """Zwroc (targety_zdefiniowane, recepty: target -> logiczne linie recepty).

    Logiczna linia = kontynuacje (konczace sie backslashem) sklejone w jedna,
    tak jak widzi je shell. Zmienne (`x = ...`, `x ?= ...`) nie sa targetami.
    """
    defined = set()
    recipes = {}
    current = None
    pending = None
    for raw in text.splitlines():
        if raw.startswith("\t"):
            line = raw[1:]
            if pending is not None:
                pending += " " + line.rstrip("\\").strip()
            else:
                pending = line.rstrip("\\").strip()
            if not raw.endswith("\\"):
                if current is not None and pending:
                    recipes[current].append(pending)
                pending = None
            continue
        pending = None
        # target: tylko gdy po nazwie nastepuje ':' i to nie jest przypisanie.
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):(?!=)\s*", raw)
        if match and not re.match(r"^[^:=]+[:?+]?=", raw):
            current = match.group(1)
            defined.add(current)
            recipes.setdefault(current, [])
    return defined, recipes


def sub_make_targets(lines):
    """Kolejnosc celow wywolywanych przez $(MAKE) w receptach."""
    invoked = []
    for line in lines:
        for match in re.finditer(r"\$\(MAKE\)\s+([A-Za-z0-9_.-]+)", line):
            invoked.append(match.group(1))
    return invoked


class ClusterBuildContractTests(unittest.TestCase):
    def setUp(self):
        self.text = makefile_text()
        _, recipes = parse_makefile(self.text)
        self.lines = recipes.get("cluster-build", [])
        self.joined = "\n".join(self.lines)

    def test_help_is_default_goal(self):
        self.assertRegex(
            self.text,
            r"(?m)^\.DEFAULT_GOAL\s*:?=\s*help\s*$",
            "samo `make` musi pokazac help, nie uruchamiac destrukcyjnego galera-rebuild",
        )

    def test_target_exists_and_is_phony(self):
        self.assertTrue(self.lines, "cluster-build musi istniec z niepusta recepta")
        phony = phony_blob(self.text)
        self.assertIsNotNone(phony, "Makefile musi miew .PHONY")
        self.assertIn("cluster-build", phony, "cluster-build musi byc w .PHONY")

    def test_orchestrates_existing_targets_only(self):
        invoked = sub_make_targets(self.lines)
        self.assertTrue(invoked, "cluster-build musi wywolywac istniejace cele przez $(MAKE)")
        defined, _ = parse_makefile(self.text)
        for target in invoked:
            self.assertIn(
                target,
                defined,
                f"cluster-build orchestruje nieistniejacy cel: {target}",
            )

    def test_core_pipeline_order(self):
        invoked = sub_make_targets(self.lines)
        positions = []
        for step in CORE_BUILD_STEPS:
            self.assertIn(step, invoked, f"cluster-build pomija krok: {step}")
            positions.append(invoked.index(step))
        self.assertEqual(
            positions,
            sorted(positions),
            f"kolejnosc krokow builda niezgodna z grafem: {invoked}",
        )

    def test_conditional_steps_between_core_and_gate(self):
        """Kroki warunkowe leza miedzy ostatnim krokiem rdzenia a brama.

        Kotwica jest wyliczana z `CORE_BUILD_STEPS`, nie wpisana na sztywno:
        poprzednia wersja pinowala `cluster-endpoint` i przy wyniesieniu VIP-a
        do warstwy wspolnej test pekal na nazwie kroku zamiast na kolejnosci,
        ktorej faktycznie broni.
        """
        invoked = sub_make_targets(self.lines)
        self.assertIn(BUILD_GATE, invoked)
        last_core = CORE_BUILD_STEPS[-1]
        for step in CONDITIONAL_BUILD_STEPS + BACKUP_BUILD_STEPS:
            self.assertIn(step, invoked, f"krok warunkowy {step} musi byc w grafie builda")
            self.assertLess(
                invoked.index(last_core),
                invoked.index(step),
                f"{step} musi isc PO {last_core}",
            )
            self.assertLess(
                invoked.index(step),
                invoked.index(BUILD_GATE),
                f"{step} musi isc PRZED {BUILD_GATE}",
            )

    def test_backup_step_materializes_gate_evidence_in_order(self):
        invoked = sub_make_targets(self.lines)
        positions = [invoked.index(step) for step in BACKUP_BUILD_STEPS]
        self.assertEqual(
            positions,
            sorted(positions),
            "backup w cluster-build musi wykonac configure, backup, drill i refresh",
        )

    def test_gate_is_the_last_step(self):
        invoked = sub_make_targets(self.lines)
        self.assertEqual(invoked[-1], BUILD_GATE, "build konczy sie bramka stanu ustalonego")

    def test_conditional_steps_are_skippable(self):
        self.assertIn(
            "BUILD_SKIP",
            self.joined,
            "kroki warunkowe (seed/backup/alerts/app-host) musza dac sie pominac bez edycji Makefile",
        )
        self.assertIn("filter-out", self.joined, "pomijanie krokow przez filtr listy krokow")

    def test_cluster_and_confirm_guards(self):
        self.assertIn(
            "$(cluster_guard)",
            self.joined,
            "cluster-build jest mutujacy — wymaga jawnego CLUSTER=",
        )
        self.assertIn(
            'test "$(CONFIRM)" = "yes"',
            self.joined,
            "cluster-build zawiera bootstrap — wymaga CONFIRM=yes",
        )

    def test_propagates_and_stops_on_first_error(self):
        self.assertTrue(self.lines)
        for line in self.lines:
            if "$(MAKE)" not in line:
                continue
            self.assertFalse(
                line.lstrip().startswith("-"),
                f"krok buildu nie moze ignorowac bledu (-): {line}",
            )
            self.assertNotIn(
                "|| true",
                line,
                f"krok buildu nie moze maskowac bledu (|| true): {line}",
            )
            if ";;" in line or re.search(r"\bfor\b|\bcase\b", line):
                make_calls = line.count("$(MAKE)")
                exit_guards = line.count("|| exit 1")
                self.assertEqual(
                    exit_guards,
                    make_calls,
                    f"kazde $(MAKE) w petli/case wymaga wlasnego || exit 1: {line}",
                )

    def test_no_second_firewall_step(self):
        # cluster-deploy juz robi f2_install + site + firewall.yml.
        invoked = sub_make_targets(self.lines)
        self.assertNotIn(
            "cluster-firewall",
            invoked,
            "cluster-deploy juz zawiera firewall — zadnego drugiego kroku firewall w buildzie",
        )
        self.assertNotIn(
            "playbooks/firewall.yml",
            self.joined,
            "cluster-build nie wywoluje firewall.yml bezposrednio (robi to cluster-deploy)",
        )


class ClusterRecoverMakefileContractTests(unittest.TestCase):
    def setUp(self):
        self.text = makefile_text()
        _, recipes = parse_makefile(self.text)
        self.lines = recipes.get("cluster-recover", [])
        self.joined = "\n".join(self.lines)

    def test_target_exists_and_is_phony(self):
        self.assertTrue(self.lines, "cluster-recover musi istniec z niepusta recepta")
        phony = phony_blob(self.text)
        self.assertIn("cluster-recover", phony, "cluster-recover musi byc w .PHONY")
        self.assertNotIn(
            "cluster-restart",
            parse_makefile(self.text)[0],
            "mylaca nazwa cluster-restart nie moze zostac jako alias",
        )

    def test_is_cluster_and_confirm_gated(self):
        self.assertIn("$(cluster_guard)", self.joined)
        self.assertIn(
            'test "$(CONFIRM)" = "yes"',
            self.joined,
            "cold recovery jest destrukcyjne — wymaga CONFIRM=yes",
        )

    def test_uses_dedicated_stop_and_selection_playbook(self):
        self.assertIn("playbooks/cluster_recover.yml", self.joined)

    def test_passes_state_file_and_bootstrap_node_override(self):
        self.assertIn(
            "recover_state_file=$(RECOVER_STATE_FILE)",
            self.joined,
            "playbook zapisuje wybrany wezel do jawnej sciezki odczytywanej przez Makefile",
        )
        self.assertIn(
            "-e recover_bootstrap_node=$(BOOTSTRAP_NODE)",
            self.joined,
            "BOOTSTRAP_NODE operatora musi trafiac do playboku jako jawny override",
        )

    def test_bootstrap_node_is_read_after_selection_playbook(self):
        active_make = "\n".join(
            line
            for line in self.text.splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertNotRegex(
            active_make,
            r"\$\(\s*shell\s+cat\b",
            "$(shell cat ...) jest rozwijane przed powstaniem pliku stanu",
        )
        self.assertIn(
            '-e bootstrap_node=$$(cat "$(RECOVER_STATE_FILE)")',
            self.joined,
            "shell ma odczytac wybor na linii bootstrapu, po zakonczeniu playbooka",
        )
        self.assertIn(
            'join_bootstrap_node=$$(cat "$(RECOVER_STATE_FILE)")',
            self.joined,
            "join musi pominac faktyczny bootstrap, nie zawsze galera[0]",
        )

    def test_reuses_canonical_bootstrap_playbook(self):
        invoked = sub_make_targets(self.lines)
        self.assertIn(
            "cluster-bootstrap",
            invoked,
            "recovery reuse'uje istniejacy cel cluster-bootstrap (kanoniczny bootstrap.yml)",
        )
        self.assertIn("bootstrap_node=", self.joined, "wybrany wezel idzie do bootstrap.yml jako bootstrap_node")
        self.assertIn(
            "bootstrap_confirm_all_down=true",
            self.joined,
            "po potwierdzeniu awarii bootstrap dostaje bootstrap_confirm_all_down=true",
        )
        self.assertNotIn("--wsrep-new-cluster", self.joined)
        self.assertNotIn("galera_new_cluster", self.joined)

    def test_join_and_health_after_bootstrap(self):
        invoked = sub_make_targets(self.lines)
        self.assertIn("cluster-join", invoked)
        self.assertLess(
            invoked.index("cluster-bootstrap"),
            invoked.index("cluster-join"),
            "join nastepuje po bootstrap",
        )
        self.assertEqual(invoked[-1], "cluster-health", "recovery konczy sie kontrola zdrowia")


class ClusterRecoverPlaybookContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = REPO / "playbooks" / "cluster_recover.yml"
        cls.raw = cls.path.read_text(encoding="utf-8")
        cls.plays = yaml.safe_load(cls.raw)

    def play_text(self, play):
        return yaml.safe_dump(play)

    def find_play(self, needle):
        for index, play in enumerate(self.plays):
            if needle in self.play_text(play):
                return index
        return None

    def test_playbook_exists_and_parses(self):
        self.assertTrue(self.plays, "cluster_recover.yml musi byc poprawnym playbookiem")

    def test_no_duplicated_bootstrap_logic(self):
        self.assertNotIn("--wsrep-new-cluster", self.raw)
        self.assertNotIn("galera_new_cluster", self.raw)

    def test_playbook_has_confirm_guard_before_any_probe(self):
        probe_index = self.find_play("wsrep_cluster_status")
        probe_play = self.plays[probe_index]
        self.assertIn("pre_tasks", probe_play)
        self.assertIn("confirm is defined", self.play_text(probe_play))
        self.assertIn("confirm | bool", self.play_text(probe_play))

    def test_live_primary_guard_precedes_stop(self):
        probe_index = self.find_play("wsrep_cluster_status")
        stop_index = self.find_play("pkill -x mariadbd")
        self.assertIsNotNone(probe_index, "playbook musi sondowac wsrep na wszystkich wezlach")
        self.assertIsNotNone(stop_index, "playbook musi zatrzymywac mariadbd")
        self.assertLess(
            probe_index,
            stop_index,
            "zywy Primary jest sprawdzany PRZED zatrzymaniem wezlow",
        )
        probe_play = self.play_text(self.plays[probe_index])
        self.assertIn("rolling-restart", probe_play, "przy zywym Primary operator odsylany do rolling-restart")
        classify = next(
            task
            for task in self.plays[probe_index]["tasks"]
            if "recover_live_primary" in task.get("ansible.builtin.set_fact", {})
        )
        expression = str(
            classify["ansible.builtin.set_fact"]["recover_live_primary"]
        )
        self.assertIn(
            "map(attribute='item')",
            expression,
            "blad live Primary musi wskazac nazwy wezlow, nie powtorzyc stdout",
        )
        self.assertNotIn("map(attribute='stdout')", expression)


    def test_stop_play_is_serial_and_fail_closed(self):
        stop_index = self.find_play("pkill -x mariadbd")
        self.assertIsNotNone(stop_index)
        stop_play = self.plays[stop_index]
        self.assertEqual(str(stop_play.get("serial")), "1", "stop Galery jest serialny (serial:1)")
        self.assertEqual(
            stop_play.get("max_fail_percentage"),
            0,
            "porazka zatrzymania wezla konczy procedure (max_fail_percentage:0)",
        )
        self.assertIn(
            "grastate",
            self.play_text(stop_play),
            "stop ma byc czysty (grastate zapisywany przy zamknieciu)",
        )

    def test_no_parallel_restart_anywhere(self):
        self.assertNotIn("systemctl restart", self.raw)
        self.assertNotIn("restarted", self.raw)

    def test_selection_reads_grastate_after_stop(self):
        stop_index = self.find_play("pkill -x mariadbd")
        select_index = self.find_play("safe_to_bootstrap")
        self.assertIsNotNone(select_index, "wybor wezla czyta grastate.dat (safe_to_bootstrap)")
        self.assertIn("seqno", self.raw, "wybor wezla bierze pod uwage seqno")
        self.assertIn("grastate.dat", self.raw)
        self.assertIsNotNone(stop_index)
        self.assertLess(
            stop_index,
            select_index,
            "grastate czytany PO stopie — seqno jest ostateczny dopiero po czystym zamknieciu",
        )

    def test_selection_requires_explicit_node_on_tie(self):
        select_play = self.play_text(self.plays[self.find_play("safe_to_bootstrap")])
        self.assertIn("recover_bootstrap_node", select_play, "jawny override wezla bootstrap")
        self.assertIn(
            "BOOTSTRAP_NODE",
            select_play,
            "przy remisie (wiele safe_to_bootstrap / remis seqno) wymagaj jawnego BOOTSTRAP_NODE",
        )

    def test_state_file_contract_with_makefile(self):
        select_play = self.play_text(self.plays[self.find_play("safe_to_bootstrap")])
        self.assertIn(
            "recover_state_file",
            select_play,
            "playbook zapisuje wybrany wezel pod recover_state_file",
        )
        _, recipes = parse_makefile(makefile_text())
        recover_recipe = "\n".join(recipes.get("cluster-recover", []))
        self.assertIn(
            "recover_state_file=$(RECOVER_STATE_FILE)",
            recover_recipe,
            "Makefile przekazuje ta sama sciezke stanu, ktora zapisuje playbook",
        )


class ClusterJoinBootstrapContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(JOIN_PLAYBOOK.read_text(encoding="utf-8"))

    def find_play(self, fragment):
        return next(
            play
            for play in self.plays
            if fragment in play.get("name", "")
        )

    def test_sst_account_is_created_on_actual_bootstrap_node(self):
        account_play = self.find_play("zapewnij konto SST")
        hosts = str(account_play.get("hosts", ""))
        self.assertIn("join_bootstrap_node", hosts)
        self.assertIn("default('galera[0]')", hosts)
        self.assertNotIn("groups[", hosts)

    def test_join_skips_actual_bootstrap_node(self):
        join_play = self.find_play("dołącz węzły do Primary Component")
        skip = next(
            task
            for task in join_play.get("tasks", [])
            if task.get("ansible.builtin.meta") == "end_host"
            and "Primary" in task.get("name", "")
        )
        condition = str(skip.get("when", ""))
        self.assertNotIn("galera_node_idx", condition)
        evaluate = jinja2.Environment().compile_expression(condition)
        groups = {"galera": ["gnode1", "gnode2", "gnode3"]}

        for host in groups["galera"]:
            self.assertEqual(
                bool(
                    evaluate(
                        inventory_hostname=host,
                        join_bootstrap_node="gnode3",
                        groups=groups,
                    )
                ),
                host == "gnode3",
                f"override: skip ma objac wylacznie gnode3, nie {host}",
            )

        for host in groups["galera"]:
            self.assertEqual(
                bool(evaluate(inventory_hostname=host, groups=groups)),
                host == "gnode1",
                f"domyslnie skip ma objac wylacznie galera[0], nie {host}",
            )


class ClusterRecoverSelectionLogicTests(unittest.TestCase):
    """Wykonuje wyrazenia selekcji z playbooka na macierzy stanow."""

    @classmethod
    def setUpClass(cls):
        cls.plays = yaml.safe_load(
            (REPO / "playbooks" / "cluster_recover.yml").read_text(encoding="utf-8")
        )
        cls.selection_tasks = cls.plays[2]["tasks"]
        cls.env = jinja2.Environment()
        cls.env.filters["b64decode"] = lambda value: base64.b64decode(value).decode()
        cls.env.filters["regex_findall"] = (
            lambda value, pattern: re.findall(pattern, value)
        )
        cls.env.tests["search"] = (
            lambda value, pattern: re.search(pattern, str(value)) is not None
        )

    @classmethod
    def eval_expr(cls, expression, context):
        return cls.env.compile_expression(expression.strip())(**context)

    @classmethod
    def eval_value(cls, value, context):
        expression = value.strip()
        if expression.startswith("{{") and expression.endswith("}}"):
            expression = expression[2:-2]
        return cls.eval_expr(expression, context)

    @classmethod
    def when_matches(cls, condition, context):
        if condition is None:
            return True
        conditions = condition if isinstance(condition, list) else [condition]
        return all(bool(cls.eval_expr(item, context)) for item in conditions)

    @classmethod
    def state_results(cls, states):
        results = []
        for index, (seqno, safe_to_bootstrap) in enumerate(states, start=1):
            newline = chr(10)
            content = newline.join(
                [
                    "# GALERA saved state",
                    "version: 2.1",
                    "uuid: abc",
                    f"seqno: {seqno}",
                    f"safe_to_bootstrap: {safe_to_bootstrap}",
                ]
            ) + newline
            encoded = base64.b64encode(content.encode()).decode()
            results.append({"item": f"gnode{index}", "content": encoded})
        return results

    @classmethod
    def run_selection(cls, states, override=None):
        results = cls.state_results(states)
        context = {
            "groups": {"galera": [item["item"] for item in results]},
            "recover_grastate": {"results": results},
            "recover_state_file": "clusters/test/recover-bootstrap-node",
        }
        if override is not None:
            context["recover_bootstrap_node"] = override

        failed_assert = None
        flipped = False
        for task in cls.selection_tasks:
            set_fact = task.get("ansible.builtin.set_fact")
            if set_fact is not None:
                loop = task.get("loop")
                items = (
                    cls.eval_value(loop, context)
                    if loop is not None
                    else [None]
                )
                for item in items:
                    local_context = dict(context)
                    if item is not None:
                        local_context["item"] = item
                    if not cls.when_matches(task.get("when"), local_context):
                        continue
                    for key, value in set_fact.items():
                        context[key] = cls.eval_value(value, local_context)
                    if item is not None:
                        local_context.update(context)
                continue

            if not cls.when_matches(task.get("when"), context):
                continue

            assertion = task.get("ansible.builtin.assert")
            if assertion is not None:
                checks = assertion.get("that", [])
                checks = checks if isinstance(checks, list) else [checks]
                if not all(bool(cls.eval_expr(check, context)) for check in checks):
                    failed_assert = task["name"]
                    break
                continue

            if task.get("ansible.builtin.lineinfile") is not None:
                flipped = True

        return context.get("recover_bootstrap_choice"), flipped, failed_assert

    def test_unique_safe_to_bootstrap_wins_over_seqno(self):
        choice, flipped, failed = self.run_selection([(99, 0), (7, 1), (99, 0)])
        self.assertEqual(choice, "gnode2")
        self.assertFalse(flipped)
        self.assertIsNone(failed)

    def test_unique_highest_seqno_is_selected_and_marked_safe(self):
        choice, flipped, failed = self.run_selection([(5, 0), (8, 0), (7, 0)])
        self.assertEqual(choice, "gnode2")
        self.assertTrue(flipped)
        self.assertIsNone(failed)

    def test_seqno_tie_fails_closed_and_requires_override(self):
        choice, flipped, failed = self.run_selection([(8, 0), (8, 0), (7, 0)])
        self.assertIsNone(choice)
        self.assertFalse(flipped)
        self.assertIn("Remis", failed)

    def test_multiple_safe_nodes_fails_closed(self):
        choice, flipped, failed = self.run_selection([(8, 1), (8, 1), (7, 0)])
        self.assertIsNone(choice)
        self.assertFalse(flipped)
        self.assertIn("Remis", failed)

    def test_explicit_override_wins_and_is_validated(self):
        choice, flipped, failed = self.run_selection([(8, 0), (8, 0), (7, 0)], "gnode2")
        self.assertEqual(choice, "gnode2")
        self.assertTrue(flipped)
        self.assertIsNone(failed)

    def test_malformed_seqno_fails_closed(self):
        choice, flipped, failed = self.run_selection([(None, 0), (8, 0), (7, 0)])
        self.assertIsNone(choice)
        self.assertFalse(flipped)
        self.assertIn("poprawny seqno", failed)

    def test_negative_seqno_requires_wsrep_recover_without_override(self):
        choice, flipped, failed = self.run_selection([(-1, 0), (8, 0), (7, 0)])
        self.assertIsNone(choice)
        self.assertFalse(flipped)
        self.assertIn("wsrep-recover", failed)

    def test_negative_seqno_accepts_explicit_recovered_node(self):
        choice, flipped, failed = self.run_selection(
            [(-1, 0), (8, 0), (7, 0)],
            "gnode1",
        )
        self.assertEqual(choice, "gnode1")
        self.assertTrue(flipped)
        self.assertIsNone(failed)


    def test_explicit_lower_seqno_override_fails_closed(self):
        choice, flipped, failed = self.run_selection([(5, 0), (8, 0), (7, 0)], "gnode3")
        self.assertIsNone(choice)
        self.assertFalse(flipped)
        self.assertIn("Jawny override", failed)



@unittest.skipIf(shutil.which("make") is None, "make niedostepny")
class MakefileDryRunGraphTests(unittest.TestCase):
    """make -n: graf bez wykonania (zadne polecenie mutujace nie startuje)."""

    def run_make(self, target, *extra):
        return subprocess.run(
            ["make", "-n", target, "CLUSTER=dry-run-probe", "CONFIRM=yes", *extra],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_cluster_build_graph_dry_run(self):
        proc = self.run_make("cluster-build")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = (
            CORE_BUILD_STEPS
            + CONDITIONAL_BUILD_STEPS
            + BACKUP_BUILD_STEPS
            + [BUILD_GATE]
        )
        for step in expected:
            self.assertIn(step, proc.stdout, f"make -n cluster-build pokazuje krok {step}")

    def test_cluster_build_skip_dry_run(self):
        proc = self.run_make(
            "cluster-build",
            "BUILD_SKIP=seed backup app-host",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(
            "for step in alerts",
            proc.stdout,
            "BUILD_SKIP wyklucza kroki z listy warunkowej",
        )

    def test_cluster_recover_graph_dry_run(self):
        proc = self.run_make("cluster-recover")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("playbooks/cluster_recover.yml", proc.stdout)
        self.assertIn("bootstrap_confirm_all_down=true", proc.stdout)
        self.assertIn("cluster-join", proc.stdout)

    def test_cluster_recover_bootstrap_node_override_dry_run(self):
        proc = self.run_make("cluster-recover", "BOOTSTRAP_NODE=dry-node-2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("-e recover_bootstrap_node=dry-node-2", proc.stdout)

if __name__ == "__main__":
    unittest.main()
