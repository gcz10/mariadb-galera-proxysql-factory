#!/usr/bin/env python3
"""O rygorze walidacji lockfile'a decyduje UŻYCIE, nie stopka pliku.

Wcześniej `validate-lockfile.py` wybierał ścieżkę łagodną wyłącznie na
podstawie samodeklaracji `# Status:` w stopce. Plik ze stopką `candidate`
przechodził bez bramki ISC-63 (placeholdery), bez kompletu wymaganych kluczy
i bez kontroli proweniencji — NAWET gdy wskazywał go `versions.lock_file`
w definicji klastra. CI waliduje lockfile'e po referencji, więc taki plik był
sprawdzany i meldował zielono; fail-closed przychodził dopiero na hoście.
"""

from __future__ import annotations

import importlib.util
import unittest
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tests" / "validation" / "validate-lockfile.py"

_spec = importlib.util.spec_from_file_location("validate_lockfile_under_test", SCRIPT)
validator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(validator)

# Kandydat w naturalnym stanie: stopka `candidate`, placeholder czekający na F0
# i niekompletne sekcje. Dokładnie taki plik żyje dziś w `versions/`.
CANDIDATE = """# Status: candidate
mariadb:
  version: "11.8.9"
  repo_setup_args: "--mariadb-server-version=11.8"
  rpm_release: "to-confirm-F0"
"""


def write_candidate(root: Path) -> Path:
    (root / "versions").mkdir(parents=True, exist_ok=True)
    path = root / "versions" / "candidate.lock.yml"
    path.write_text(CANDIDATE, encoding="utf-8")
    return path


def write_cluster(root: Path, name: str, lock_file: str | None) -> None:
    cluster_dir = root / "clusters" / name
    cluster_dir.mkdir(parents=True, exist_ok=True)
    body: dict = {"cluster": {"name": name}}
    if lock_file is not None:
        body["versions"] = {"lock_file": lock_file}
    (cluster_dir / "cluster.yml").write_text(yaml.safe_dump(body), encoding="utf-8")


class LockfileStrictnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lockfile-strict-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.candidate = write_candidate(self.root)

    def errors_for(self, referenced):
        return validator.validate(self.candidate, referenced)

    def test_unused_candidate_keeps_the_lenient_path(self):
        """Szkic, którym nikt nie buduje, ma prawo być niekompletny."""
        write_cluster(self.root, "other", "versions/versions.lock.yml")
        referenced = validator.referenced_lockfiles(self.root)
        self.assertNotIn(self.candidate.resolve(), referenced)
        self.assertEqual(self.errors_for(referenced), [])

    def test_referenced_candidate_gets_the_full_rigour(self):
        """Wskazany kandydat przestaje być szkicem: ISC-63 i komplet kluczy."""
        write_cluster(self.root, "tenant", "versions/candidate.lock.yml")
        referenced = validator.referenced_lockfiles(self.root)
        self.assertIn(self.candidate.resolve(), referenced)

        errors = "\n".join(self.errors_for(referenced))
        self.assertIn("to-confirm-f0", errors)          # bramka ISC-63
        self.assertIn("brak sekcji 'proxysql'", errors)  # komplet kluczy
        self.assertIn("Status: LOCKED", errors)          # stopka nie zgadza się z użyciem

    def test_reference_scan_reads_the_declaration(self):
        write_cluster(self.root, "with-lock", "versions/versions-el10.lock.yml")
        write_cluster(self.root, "without-lock", None)
        self.assertEqual(
            validator.referenced_lockfiles(self.root),
            {(self.root / "versions/versions-el10.lock.yml").resolve()},
        )

    def test_broken_cluster_definition_does_not_hide_references(self):
        """Zepsuty `cluster.yml` to problem walidatora klastra — nie może
        wyciszyć rygoru dla lockfile'a wskazanego przez pozostałe definicje."""
        write_cluster(self.root, "tenant", "versions/candidate.lock.yml")
        broken = self.root / "clusters" / "broken"
        broken.mkdir(parents=True, exist_ok=True)
        (broken / "cluster.yml").write_text("cluster: [unclosed\n", encoding="utf-8")
        self.assertIn(
            self.candidate.resolve(),
            validator.referenced_lockfiles(self.root),
        )


if __name__ == "__main__":
    unittest.main()
