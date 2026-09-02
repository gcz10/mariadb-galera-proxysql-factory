#!/usr/bin/env python3
"""Folder alertów w Grafanie musi byc rozlaczny miedzy najemcami.

POWSTAL Z DEFEKTU (vega-r9, 2026-08-25): szablon wysylal
`monitoring.alerts.folder_uid: "isa-alerts"` — te sama wartosc dla KAZDEGO
klastra. Tytul folderu jest per najemca (`ISA Alerts (<cluster_name>)`), a krok
F15 szuka istniejacego folderu WLASNIE po tytule. Drugi najemca tego samego PMM
nie znajdowal wiec swojego tytulu, probowal utworzyc folder z zajetym `uid`
i dostawal od Grafany 412 Precondition Failed — po dziesieciu minutach budowy.

Playbook od poczatku umial wyprowadzic `isa-alerts-<cluster_name>`, ale robil to
tylko dla wartosci NIEZDEFINIOWANEJ. Szablon ja definiowal, wiec bezpieczne
wyprowadzenie nigdy nie dochodzilo do glosu.

Te testy pilnuja obu polowek naprawy: szablon nie moze narzucac wspolnego uid,
a playbook musi traktowac pusta wartosc jak jej brak.
"""

import sys
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CLUSTERS = REPO / "clusters"
F15 = REPO / "playbooks" / "f15_alerts.yml"
ALERT_RULES = REPO / "playbooks" / "vars" / "alert_rules.yml"
LAB = REPO / "tests" / "lab"
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))
from alert_identity import alert_uid_prefixes  # noqa: E402


def _cluster_files():
    for path in sorted(CLUSTERS.glob("*/cluster.yml")):
        yield path, yaml.safe_load(path.read_text(encoding="utf-8"))

def _rule_uid_templates():
    uids = []

    def _walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("uid"), str):
                uids.append(node["uid"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(yaml.safe_load(F15.read_text(encoding="utf-8")))
    if ALERT_RULES.is_file():
        _walk(yaml.safe_load(ALERT_RULES.read_text(encoding="utf-8")))
    return uids

def _effective_uid_prefix(cluster_label):
    return alert_uid_prefixes(cluster_label)[0]




class AlertFolderTenancyTests(unittest.TestCase):
    def test_template_does_not_ship_a_shared_folder_uid(self):
        """Wartosc w szablonie staje sie wartoscia u KAZDEGO, kto go skopiuje."""
        cfg = yaml.safe_load((CLUSTERS / "example-cluster" / "cluster.yml").read_text(encoding="utf-8"))
        uid = (cfg.get("monitoring", {}).get("alerts", {}) or {}).get("folder_uid", "")
        self.assertEqual(
            uid, "",
            "szablon narzuca wspolny folder_uid — drugi najemca tego samego PMM "
            "dostanie 412 przy tworzeniu folderu alertow",
        )

    def test_no_two_clusters_declare_the_same_folder_uid(self):
        """Jawna wartosc jest dozwolona, ale musi byc unikalna w calej flocie."""
        seen = {}
        for path, cfg in _cluster_files():
            uid = ((cfg or {}).get("monitoring", {}).get("alerts", {}) or {}).get("folder_uid", "")
            if not uid:
                continue
            self.assertNotIn(
                uid, seen,
                f"{path} i {seen.get(uid)} deklaruja ten sam folder_uid={uid!r}",
            )
            seen[uid] = path

    def test_playbook_treats_empty_uid_as_absent(self):
        """`default(x)` przepuszcza pusty string; potrzebne `default(x, true)`."""
        text = F15.read_text(encoding="utf-8")
        self.assertIn(
            "default('isa-alerts-' ~ cluster_label, true)", text,
            "puste folder_uid nie wpadnie na wyprowadzenie per najemca",
        )

    def test_folder_title_stays_tenant_scoped(self):
        """Tytul i uid musza byc rozlaczne TA SAMA os — inaczej wracamy do 412."""
        text = F15.read_text(encoding="utf-8")
        self.assertIn('f15_folder_title: "ISA Alerts ({{ cluster_label }})"', text)

    def test_every_rendered_rule_uid_fits_grafana_limit(self):
        """Grafana odrzuca provisioning UID dluzszego niz 40 znakow."""
        templates = _rule_uid_templates()
        self.assertTrue(templates)
        for path, cfg in _cluster_files():
            label = ((cfg or {}).get("monitoring", {}).get("pmm", {}) or {}).get(
                "cluster_name", ""
            )
            if not label:
                continue
            for template in templates:
                uid = (
                    template.replace("{{ cluster_label }}", label)
                    .replace("{{ f15_uid_prefix }}", _effective_uid_prefix(label))
                )
                self.assertLessEqual(
                    len(uid),
                    40,
                    f"{path}: Grafana odrzuci UID {uid!r} ({len(uid)} znakow)",
                )


if __name__ == "__main__":
    unittest.main()
