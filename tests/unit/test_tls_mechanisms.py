"""Testy mechanik TLS: wystawianie lisci per wezel i rotacja CA.

Wszystko dzieje sie w katalogu tymczasowym — skrypty sa kopiowane do tmp i
wywolywane wzgledem swojej kopii, wiec zywe katalogi materialu (n15, fc9, ...)
i infrastruktura sa nietkane. To testy MECHANIK openssl/bash, nie wdrozenia:

  * trust-both buduje bundle dokladnie dwoch CA (okno podwojnego zaufania),
  * issue-node-certs.sh przyjmuje jawne CA_FILE/CA_KEY i wystawia lisc od
    wskazanego CA (a bundle wielu CA odrzuca — podpis pod bundlem jest dwuznaczny),
  * reissue rotacji CA przestawia issuer KAZDEGO liscia per wezel na nowe CA,
  * retire-old zdejmuje stare CA tylko wtedy, gdy caly wdrazany material
    (liscie per wezel; w trybie wspolnym server-cert) weryfikuje sie nowym CA.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TLS_SCRIPTS = REPO_ROOT / "tests" / "lab" / "tls"
TLS_ROTATE_PLAYBOOK = REPO_ROOT / "playbooks" / "tls_rotate.yml"

HOSTS = ("nlabg1", "nlabg2")
IPS = {"nlabg1": "10.0.0.1", "nlabg2": "10.0.0.2"}
CN = "nlab-galera"  # generate.sh/rotate-ca.sh mapuja go na katalog "nlab"


class TlsMechanismsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lab = Path(tmp.name) / "tls"
        self.lab.mkdir()
        # Kopie skryptow do tmp: DIR w skryptach jest wywodzony od polozenia
        # BASH_SOURCE, wiec kopie pracuja w izolacji od repozytorium.
        for script in ("generate.sh", "issue-node-certs.sh", "rotate-ca.sh"):
            shutil.copy2(TLS_SCRIPTS / script, self.lab / script)
        self.dir = self.lab / "nlab"
        # Stan wyjsciowy: wspolne CA + server-cert (tryb wspolny) oraz liscie
        # per wezel spod tego CA — jak po generate.sh + issue-node-certs.sh.
        res = self.run_script("generate.sh", CN, "nlabg1,nlabg2,10.0.0.1,10.0.0.2")
        self.assertEqual(res.returncode, 0, res.stderr)
        res = self.run_script(
            "issue-node-certs.sh", "nlab", "nlabg1=10.0.0.1,nlabg2=10.0.0.2", "90"
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    # --- helpery -----------------------------------------------------------

    def run_script(self, script, *args, **kwargs):
        env = dict(os.environ)
        env.update(kwargs.get("env_extra") or {})
        return subprocess.run(
            ["bash", str(self.lab / script), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def rotate(self, phase):
        return self.run_script("rotate-ca.sh", CN, "nlabg1,nlabg2,10.0.0.1,10.0.0.2", phase)

    def openssl(self, *args):
        return subprocess.run(
            ["openssl", *args], capture_output=True, text=True
        )

    def verify(self, ca_pem, cert_pem):
        """Prawda, gdy cert weryfikuje sie pod CA (openssl verify)."""
        return (
            self.openssl("verify", "-CAfile", str(ca_pem), str(cert_pem)).returncode == 0
        )

    def cert_count(self, pem):
        return pem.read_text().count("-----BEGIN CERTIFICATE-----")

    def x509_name(self, pem, field):
        """issuer/subject po zrzuceniu prefiksu pola i normalizacji bialych znakow.

        LibreSSL drukuje 'issuer= /CN=x', OpenSSL 3 'issuer=CN = x' — obcinamy
        wszystko do pierwszego '=', obie strony porownania rysuje ten sam binarium.
        """
        out = self.openssl("x509", "-in", str(pem), "-noout", "-" + field)
        self.assertEqual(out.returncode, 0, out.stderr)
        value = out.stdout.strip()
        value = value.split("=", 1)[1] if "=" in value else value
        return " ".join(value.split())

    def node_cert(self, host):
        return self.dir / f"node-{host}-cert.pem"

    def issue_leaf_from(self, ca_pem, ca_key, host):
        """Podpisuje lisc per wezel danym CA na czystym openssl (bez skryptow).

        Sluzy do symulacji STAREGO liscia (podpisany starym CA) w stanie, w ktorym
        reszta materialu jest juz od nowego CA — skrypty same takiego stanu nie
        wytworza, a wlasnie taki stan musi odrzucic brama retire-old.
        """
        csr = self.dir / f"{host}.csr"
        ext = self.dir / f"{host}.ext"
        ext.write_text(
            "subjectAltName=DNS:%s,IP:%s\n"
            "extendedKeyUsage=serverAuth,clientAuth\n"
            "basicConstraints=CA:FALSE\n" % (host, IPS[host])
        )
        with open(os.devnull, "wb") as devnull:
            subprocess.check_call(
                [
                    "openssl", "req", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(self.dir / f"node-{host}-key.pem"),
                    "-out", str(csr), "-subj", f"/CN={host}",
                ],
                stdout=devnull, stderr=devnull,
            )
            subprocess.check_call(
                [
                    "openssl", "x509", "-req", "-in", str(csr), "-sha256",
                    "-days", "30", "-CA", str(ca_pem), "-CAkey", str(ca_key),
                    "-CAcreateserial", "-out", str(self.node_cert(host)),
                    "-extfile", str(ext),
                ],
                stdout=devnull, stderr=devnull,
            )
        csr.unlink()
        ext.unlink()

    # --- trust-both --------------------------------------------------------

    def test_trust_both_produces_two_cert_bundle(self):
        res = self.rotate("trust-both")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(
            self.cert_count(self.dir / "ca.pem"),
            2,
            "okno podwojnego zaufania = bundle dokladnie dwoch CA",
        )

    # --- issue-node-certs.sh: jawne CA_FILE/CA_KEY --------------------------

    def test_explicit_ca_file_issues_leaf_from_new_ca(self):
        """CA_FILE/CA_KEY wskazuja CA podpisujace — tu: nowe CA z okna rotacji."""
        self.rotate("trust-both")  # tworzy ca-next.pem/ca-next-key.pem
        res = self.run_script(
            "issue-node-certs.sh", "nlab", "nlabg1=10.0.0.1", "90",
            env_extra={
                "CA_FILE": str(self.dir / "ca-next.pem"),
                "CA_KEY": str(self.dir / "ca-next-key.pem"),
            },
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        cert = self.node_cert("nlabg1")
        self.assertTrue(cert.exists())
        self.assertTrue(
            self.verify(self.dir / "ca-next.pem", cert),
            "lisc ma byc podpisany NOWYM CA (CA_FILE), nie domyslnym ca.pem",
        )
        self.assertEqual(
            self.x509_name(cert, "issuer"),
            self.x509_name(self.dir / "ca-next.pem", "subject"),
        )

    def test_issue_node_certs_rejects_bundle_ca_file(self):
        """W oknie podwojnego zaufania ca.pem jest bundlem — podpis pod nim jest
        dwuznaczny (openssl bierze pierwszy cert z bundle'a), wiec ma FAILowac."""
        self.rotate("trust-both")  # domyslny ca.pem staje sie bundlem 2 CA
        res = self.run_script("issue-node-certs.sh", "nlab", "nlabg3=10.0.0.3", "90")
        self.assertNotEqual(
            res.returncode, 0,
            "issue-node-certs.sh ma odmowic podpisu, gdy CA_FILE zawiera wiecej niz jeden cert",
        )
        self.assertFalse(
            (self.dir / "node-nlabg3-cert.pem").exists(),
            "odmowa ma nastapic PRZED wystawieniem jakiegokolwiek liscia",
        )

    # --- reissue: wszystkie liscie per wezel od nowego CA --------------------

    def test_reissue_rotates_issuers_of_all_node_leafs(self):
        old_ca_subject = self.x509_name(self.dir / "ca.pem", "subject")
        self.rotate("trust-both")
        res = self.rotate("reissue")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        next_subject = self.x509_name(self.dir / "ca-next.pem", "subject")
        self.assertNotEqual(old_ca_subject, next_subject)
        for host in HOSTS:
            cert = self.node_cert(host)
            self.assertEqual(
                self.x509_name(cert, "issuer"),
                next_subject,
                f"{host}: issuer liscia per wezel ma byc NOWE CA",
            )
            self.assertTrue(
                self.verify(self.dir / "ca-next.pem", cert),
                f"{host}: lisc ma weryfikowac sie nowym CA",
            )

    # --- retire-old: jedna brama dla calego wdrazanego materialu -------------

    def test_retire_old_leaves_single_ca_trusted_by_all_nodes(self):
        self.rotate("trust-both")
        res = self.rotate("reissue")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        res = self.rotate("retire-old")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        ca = self.dir / "ca.pem"
        self.assertEqual(self.cert_count(ca), 1, "po retire-old zaufanie = tylko nowe CA")
        for host in HOSTS:
            self.assertTrue(
                self.verify(ca, self.node_cert(host)),
                f"{host}: po zdjeciu starego CA lisc per wezel ma sie weryfikowac",
            )
        # Tryb wspolny: server-cert pozostaje pelnoprawnym materialem.
        self.assertTrue(self.verify(ca, self.dir / "server-cert.pem"))

    def test_retire_old_refuses_when_node_leaf_not_reissued(self):
        """Brama z dokumentacji: "Do not remove the old CA from trust until every
        node has been reissued" — jeden STARY lisc per wezel blokuje retire-old."""
        self.rotate("trust-both")
        res = self.rotate("reissue")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        # Symulacja wezla, ktory nie zostal reissue'niety: jego lisc per wezel
        # nadal pochodzi od STAREGO CA (tu: ca-previous.pem z okna rotacji).
        self.issue_leaf_from(
            self.dir / "ca-previous.pem", self.dir / "ca-key.pem", HOSTS[0]
        )
        res = self.rotate("retire-old")
        self.assertNotEqual(
            res.returncode, 0,
            "retire-old ma odmowic, gdy ktorykolwiek wdrazany lisc per wezel "
            "nie weryfikuje sie nowym CA",
        )
        self.assertEqual(
            self.cert_count(self.dir / "ca.pem"), 2,
            "po odmowie bundle ma zostac nietkniety — stare CA jeszcze potrzebne",
        )

    def test_shared_mode_rotation_without_node_leafs(self):
        """Tryb wspolny (brak lisci per wezel): rotacja ma dzialac jak wczesniej —
        reissue wystawia server-cert, retire-old zamyka okno na jednym CA."""
        res = self.run_script("generate.sh", "nlsh-galera", "nlshg1,nlshg2,10.1.0.1,10.1.0.2")
        self.assertEqual(res.returncode, 0, res.stderr)
        shared = self.lab / "nlsh"
        for phase in ("trust-both", "reissue", "retire-old"):
            res = self.run_script(
                "rotate-ca.sh", "nlsh-galera", "nlshg1,nlshg2,10.1.0.1,10.1.0.2", phase
            )
            self.assertEqual(res.returncode, 0, f"{phase}: {res.stderr}{res.stdout}")
        ca = shared / "ca.pem"
        self.assertEqual(self.cert_count(ca), 1)
        self.assertTrue(self.verify(ca, shared / "server-cert.pem"))
        self.assertFalse(list(shared.glob("node-*-cert.pem")))


class TlsDeploymentContractTests(unittest.TestCase):
    def test_ca_rotation_updates_application_trust_in_same_run(self):
        plays = yaml.safe_load(TLS_ROTATE_PLAYBOOK.read_text(encoding="utf-8"))
        app_plays = [play for play in plays if play.get("hosts") == "app"]
        self.assertEqual(
            len(app_plays),
            1,
            "rotacja CA musi aktualizowac zaufanie klientow grupy app",
        )
        includes = [
            task
            for task in app_plays[0].get("tasks", [])
            if task.get("ansible.builtin.include_tasks") == "tls_certs.yml"
        ]
        self.assertEqual(len(includes), 1)
        vars_ = includes[0].get("vars", {})
        self.assertEqual(vars_.get("tls_file_owner"), "root")
        self.assertEqual(vars_.get("tls_deploy_key"), False)
        self.assertEqual(
            vars_.get("tls_dir"),
            "/etc/mysql/app/{{ cluster.name }}",
        )
        condition = str(includes[0].get("when", ""))
        self.assertIn(".mode", condition)
        self.assertIn("'full'", condition)


if __name__ == "__main__":
    unittest.main()
