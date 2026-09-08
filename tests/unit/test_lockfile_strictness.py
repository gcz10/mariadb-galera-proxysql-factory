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


def write_cluster(root: Path, name: str, lock_file: str | None, rocky_linux_major: int | None = None) -> Path:
    cluster_dir = root / "clusters" / name
    cluster_dir.mkdir(parents=True, exist_ok=True)
    body: dict = {"cluster": {"name": name}}
    if rocky_linux_major is not None:
        body["platform"] = {"rocky_linux_major": rocky_linux_major}
    if lock_file is not None:
        body["versions"] = {"lock_file": lock_file}
    path = cluster_dir / "cluster.yml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def write_platform(root: Path, name: str, lock_file: str | None, rocky_linux_major: int | None = None) -> Path:
    platform_dir = root / "platform" / name
    platform_dir.mkdir(parents=True, exist_ok=True)
    body: dict = {"platform": {"name": name}}
    if rocky_linux_major is not None:
        body["platform"]["rocky_linux_major"] = rocky_linux_major
    if lock_file is not None:
        body["versions"] = {"lock_file": lock_file}
    path = platform_dir / "platform.yml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path

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

    def test_complete_candidate_in_use_is_not_punished_for_its_label(self):
        """Rygor pilnuje TREŚCI, nie etykiety: kompletny kandydat przechodzi.

        To rozstrzyga wariant (a) wobec (b): wskazanie pliku ze stopką
        `candidate` NIE jest samo w sobie błędem, więc promocja pinów
        testowana na jednym klastrze pozostaje możliwa.
        """
        complete = yaml.safe_load(
            (REPO / "versions" / "versions.lock.yml").read_text(encoding="utf-8")
        )
        promoted = self.root / "versions" / "promoted.lock.yml"
        promoted.write_text("# Status: candidate\n" + yaml.safe_dump(complete), encoding="utf-8")
        write_cluster(self.root, "tenant", "versions/promoted.lock.yml")

        referenced = validator.referenced_lockfiles(self.root)
        self.assertIn(promoted.resolve(), referenced)
        self.assertEqual(validator.validate(promoted, referenced), [])

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



class LockfileRockyAgreementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="lockfile-rocky-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "versions").mkdir(parents=True, exist_ok=True)
        self.lock_el9 = self.root / "versions" / "versions-el9.lock.yml"
        el9_content = yaml.safe_load((REPO / "versions" / "versions.lock.yml").read_text(encoding="utf-8"))
        self.lock_el9.write_text("# Status: LOCKED\n" + yaml.safe_dump(el9_content), encoding="utf-8")
        self.lock_el10 = self.root / "versions" / "versions-el10.lock.yml"
        el10_content = yaml.safe_load((REPO / "versions" / "versions-el10.lock.yml").read_text(encoding="utf-8"))
        self.lock_el10.write_text("# Status: LOCKED\n" + yaml.safe_dump(el10_content), encoding="utf-8")

    def test_cluster_rocky_major_agreement_passes(self):
        write_cluster(self.root, "c9", "versions/versions-el9.lock.yml", rocky_linux_major=9)
        referenced = validator.referenced_lockfiles(self.root)
        errs = validator.validate(self.lock_el9, referenced, repo_root=self.root)
        self.assertEqual(errs, [])

    def test_cluster_rocky_major_mismatch_fails(self):
        # Klaster deklaruje Rocky 10, ale wskazuje lockfile EL9
        write_cluster(self.root, "mismatch-cluster", "versions/versions-el9.lock.yml", rocky_linux_major=10)
        referenced = validator.referenced_lockfiles(self.root)
        errs = validator.validate(self.lock_el9, referenced, repo_root=self.root)
        self.assertTrue(
            any("platform.rocky_linux_major (10) nie zgadza sie z rocky_linux.major (9)" in e for e in errs),
            f"Nie wykryto bledu niezgodnosci rocky_linux_major: {errs}",
        )

    def test_platform_rocky_major_agreement_passes(self):
        write_platform(self.root, "p10", "versions/versions-el10.lock.yml", rocky_linux_major=10)
        referenced = validator.referenced_lockfiles(self.root)
        errs = validator.validate(self.lock_el10, referenced, repo_root=self.root)
        self.assertEqual(errs, [])

    def test_platform_rocky_major_mismatch_fails(self):
        # Platforma deklaruje Rocky 9, ale wskazuje lockfile EL10
        write_platform(self.root, "mismatch-plat", "versions/versions-el10.lock.yml", rocky_linux_major=9)
        referenced = validator.referenced_lockfiles(self.root)
        errs = validator.validate(self.lock_el10, referenced, repo_root=self.root)
        self.assertTrue(
            any("platform.rocky_linux_major (9) nie zgadza sie z rocky_linux.major (10)" in e for e in errs),
            f"Nie wykryto bledu niezgodnosci rocky_linux_major: {errs}",
        )

    def test_referenced_lockfiles_discovers_platform(self):
        write_platform(self.root, "plat", "versions/versions-el10.lock.yml", rocky_linux_major=10)
        referenced = validator.referenced_lockfiles(self.root)
        self.assertIn(self.lock_el10.resolve(), referenced)

    def test_validate_declaration_reports_mismatch(self):
        c_path = write_cluster(self.root, "bad-cluster", "versions/versions-el9.lock.yml", rocky_linux_major=10)
        errs = validator.validate_declaration(c_path, repo_root=self.root)
        self.assertTrue(
            any("nie zgadza sie z rocky_linux.major" in e for e in errs),
            f"Nie wykryto bledu niezgodnosci w validate_declaration: {errs}",
        )

    def test_validate_declaration_passes_when_matching(self):
        c_path = write_cluster(self.root, "good-cluster", "versions/versions-el9.lock.yml", rocky_linux_major=9)
        errs = validator.validate_declaration(c_path, repo_root=self.root)
        self.assertEqual(errs, [])

    def test_validate_declaration_fails_when_lock_file_missing(self):
        c_path = write_cluster(self.root, "no-lock-cluster", None, rocky_linux_major=9)
        errs = validator.validate_declaration(c_path, repo_root=self.root)
        self.assertTrue(
            any("brak versions.lock_file" in e for e in errs),
            f"Nie zgloszono bledu braku versions.lock_file: {errs}",
        )

    def test_validate_declaration_enforces_full_rigour_on_candidate(self):
        write_candidate(self.root)
        c_path = write_cluster(self.root, "cand-cluster", "versions/candidate.lock.yml", rocky_linux_major=9)
        errs = validator.validate_declaration(c_path, repo_root=self.root)
        # Jawne wskazanie w deklaracji musi wymusic rygor pelny (ISC-63 placeholdery)
        self.assertTrue(
            any("to-confirm-f0" in e for e in errs),
            f"Kandydat nie zostal objety pelnym rygorem: {errs}",
        )

if __name__ == "__main__":
    unittest.main()
