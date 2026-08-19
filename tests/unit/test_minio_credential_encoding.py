#!/usr/bin/env python3
"""Poswiadczenia MinIO nie moga byc percent-encodowane ani jechac w URL-u.

POWSTAL PO REALNEJ AWARII KONTRAKTU (n14, 2026-08-19):
`provision_minio.yml` sklejal `MC_HOST_<alias>=http://<ak>:<sk>@host` i puszczal
obie wartosci przez `| urlencode`. `mc` czyta userinfo DOSLOWNIE - nie dekoduje
percent-encoding (kanoniczny przyklad w dokumentacji MinIO ma sekret z `+`
surowy). Sekret klastra zawieral `+`, wiec `%2B` dawal SignatureDoesNotMatch.

Skutek byl cichy i kosztowny:
  * sonda "czy istniejace poswiadczenie dziala" zawsze konczyla sie rc=1,
  * `galera_backup_reuse_existing_s3_key` bylo zawsze falszem,
  * KAZDY `cluster-backup-configure` kasowal konto serwisowe MinIO i tworzyl
    nowe, przepisujac `secrets.env` na wszystkich hostach backupu,
  * "Converge policy on the existing scoped credential" nigdy nie biegalo -
    martwy kod udajacy zabezpieczenie.

Zaden test ani sonda tego nie widzialy: backup dzialal, bo swiezy klucz dostawal
poprawna polityke. Objaw pokazal dopiero drugi przebieg `configure` na zbieznym
klastrze (`changed=9` zamiast `changed=0`).

Sam URL nie wystarczy nawet bez kodowania: sekrety MinIO uzywaja alfabetu
base64, wiec moga zawierac `/`, ktore w userinfo jest nielegalne. Dlatego
poswiadczenie klastra idzie przez `mc alias set` z jawnymi argumentami, a dla
root-a obowiazuje glosna asercja zamiast cichego bledu podpisu.
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROVISION = REPO / "roles" / "galera_backup" / "tasks" / "provision_minio.yml"


class TestNoUrlencodedCredentials(unittest.TestCase):
    def setUp(self):
        self.text = PROVISION.read_text(encoding="utf-8")

    def test_no_urlencode_on_any_credential(self):
        """`urlencode` na poswiadczeniu = SignatureDoesNotMatch przy `+` w sekrecie.

        Liczy sie UZYCIE filtra, nie slowo: komentarze tlumaczace, czemu go tu nie
        ma, sa dozwolone (pierwsza wersja tego testu wywracala sie na wlasnym
        komentarzu).
        """
        offenders = [
            ln.strip()
            for ln in self.text.splitlines()
            if "| urlencode" in ln and not ln.lstrip().startswith("#")
        ]
        self.assertEqual(
            offenders,
            [],
            "mc nie dekoduje percent-encoding - urlencode psuje podpis: " + "; ".join(offenders),
        )

    def test_scoped_credential_never_lands_in_a_url(self):
        """Sekret klastra moze zawierac `/`, ktore w userinfo jest nielegalne."""
        for ln in self.text.splitlines():
            if "MC_HOST_" in ln and "existing_s3" in ln:
                self.fail(f"poswiadczenie klastra w URL-u: {ln.strip()}")

    def test_scoped_probe_uses_alias_set_with_explicit_args(self):
        self.assertIn("mc alias set scoped", self.text)
        self.assertIn('"$GALERA_MC_AK" "$GALERA_MC_SK"', self.text)

    def test_secret_is_expanded_inside_container_not_in_argv(self):
        """Rozwiniecie w powloce kontenera trzyma sekret poza `ps` na hoscie."""
        self.assertIn("GALERA_MC_SK={{ galera_backup_existing_s3_secret_key }}", self.text)
        self.assertNotIn("mc alias set scoped http://localhost:9000 {{", self.text)


class TestRootCredentialGuard(unittest.TestCase):
    def setUp(self):
        self.text = PROVISION.read_text(encoding="utf-8")

    def test_root_credentials_are_asserted_url_safe(self):
        """Zamiast cichego bledu podpisu - czytelna asercja przy provisioningu."""
        self.assertIn("Require URL-safe MinIO root credentials", self.text)
        block = self.text[self.text.index("Require URL-safe MinIO root credentials") :][:900]
        for var in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
            self.assertIn(var, block)
        self.assertRegex(block, r"is not search\(")

    def test_guard_rejects_characters_that_break_userinfo(self):
        """Wzorzec z roli jest uzywany DOSLOWNIE - zadnego tlumaczenia w tescie.

        Poprzednia wersja robila tu `.replace('[:space:]', r'\\s')` i przez to
        certyfikowala wzorzec, ktorego rola NIE uzywala: `[/@:[:space:]]` w Pythonie
        to klasa {/ @ : [ s p a c e} plus literal ']', wiec nie lapie ani `/`, ani
        `@`, ani `:`, ani spacji. Test swiecil na zielono, a asercja w roli byla
        dekoracja. Tlumaczenie wzorca w tescie = pranie testu.
        """
        block = self.text[self.text.index("Require URL-safe MinIO root credentials") :][:1200]
        m = re.search(r"is not search\('([^']+)'\)", block)
        self.assertIsNotNone(m, "nie znaleziono wzorca w asercji")
        pattern = m.group(1)
        for bad in ("se/cret", "user@host", "pa:ss", "with space"):
            self.assertRegex(bad, pattern, f"asercja przepuscilaby {bad!r}")
        for good in ("zuf+tfteSlswRu7BJ86wekitnifILbZam1KYY3TG", "9Q8X78QOJ1UGLQR2K67X"):
            self.assertNotRegex(good, pattern, f"asercja odrzucilaby poprawne {good!r}")


if __name__ == "__main__":
    unittest.main()
