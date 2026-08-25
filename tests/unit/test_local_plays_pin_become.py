#!/usr/bin/env python3
"""Play na maszynie kontrolnej nie moze dziedziczyc `ansible_become` z inwentarza.

POWSTAL Z CZYSTEGO PRZEBIEGU (sigma-r9, 2026-08-25). Szablon inwentarza
dokumentuje uzytkownika nie-root z sudo i ustawia `ansible_become: true` w
`all.vars`. Kazdy play `hosts: localhost` deklarowal `become: false`, co
WYGLADALO na wystarczajace — ale w precedencji Ansible ZMIENNA `ansible_become`
z inwentarza bije SLOWO KLUCZOWE play'a. Efekt: rejestracja uslug w PMM
probowala `sudo` na maszynie kontrolnej i konczyla sie
"sudo: a password is required", po czterech minutach udanego converge.

Klastry tego laboratorium tego nie lapaly, bo lacza sie jako root bez
`ansible_become` — czyli defekt dotykal dokladnie sciezki z README, ktorej
zaden zywy klaster nie uzywal.

Kontrakt: kazdy play `hosts: localhost` i kazdy task delegowany na localhost
przypina `ansible_become: false` w `vars`, gdzie precedencja jest wyzsza niz
inwentarz.
"""
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
PLAYBOOKS = REPO / "playbooks"


class LocalPlaysPinBecomeTests(unittest.TestCase):
    def test_every_localhost_play_pins_become_off(self):
        offenders = []
        checked = 0
        for path in sorted(PLAYBOOKS.glob("*.yml")):
            plays = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(plays, list):
                continue
            for play in plays:
                if not isinstance(play, dict) or str(play.get("hosts")) != "localhost":
                    continue
                checked += 1
                pinned = str((play.get("vars") or {}).get("ansible_become", "")).lower()
                if pinned != "false":
                    offenders.append(f"{path.name}: {play.get('name')}")
        self.assertGreater(checked, 0, "nie znalazlem zadnego play'a na localhost")
        self.assertEqual(
            offenders,
            [],
            "play na localhost bez `ansible_become: false` w vars — inwentarz "
            "z uzytkownikiem nie-root wymusi sudo na maszynie kontrolnej: "
            f"{offenders}",
        )

    def test_every_task_delegated_to_localhost_pins_become_off(self):
        offenders = []
        checked = 0

        def walk(node, path):
            nonlocal checked
            if isinstance(node, list):
                for item in node:
                    walk(item, path)
                return
            if not isinstance(node, dict):
                return
            if str(node.get("delegate_to", "")) == "localhost" or "local_action" in node:
                checked += 1
                pinned = str(
                    (node.get("vars") or {}).get("ansible_become", "")
                ).lower()
                if pinned != "false":
                    offenders.append(f"{path.name}: {node.get('name')}")
            for key in (
                "tasks", "pre_tasks", "post_tasks", "handlers",
                "block", "rescue", "always",
            ):
                walk(node.get(key), path)

        for path in sorted(PLAYBOOKS.glob("*.yml")):
            walk(yaml.safe_load(path.read_text(encoding="utf-8")), path)

        self.assertGreater(checked, 0, "nie znalazlem delegacji na localhost")
        self.assertEqual(
            offenders,
            [],
            "task delegowany na localhost dziedziczy ansible_become z inventory: "
            f"{offenders}",
        )

    def test_template_inventory_still_documents_non_root_user(self):
        """Falsyfikowalnosc: gdyby szablon przestal uzywac sudo, kontrakt bylby pusty.

        Pin ma sens tylko dopoki inwentarze moga deklarowac `ansible_become`.
        """
        template = (REPO / "clusters" / "example-cluster" / "inventory.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("ansible_become: true", template)


if __name__ == "__main__":
    unittest.main()
