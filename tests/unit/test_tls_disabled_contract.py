"""Kontrakt `tls.mode=disabled`: deklaracja "bez TLS" musi byc PRAWDZIWA.

POWSTAL Z DEFEKTU (2026-09-08, budowa najemcy bez TLS): szablon traktowal
"disabled" jako BRAK certyfikatow — nie emitowal zadnego `ssl_*`. To wystarczalo
do MariaDB 11.3. Od 11.4 serwer, ktory nie dostal certyfikatu, GENERUJE WLASNY i
szyfruje polaczenia domyslnie (Zero-Configuration SSL), wiec deklaracja
`tls.mode=disabled` dawala klaster, ktory nadal szyfrowal ruch klienta. Zrodlo:
mariadb.com/docs/server/security/encryption/data-in-transit-encryption/
zero-configuration-ssl — tabela "Configuration Options", `--disable-ssl`.

Falsyfikowalnosc: usun `disable-ssl` z szablonu, a `test_disabled_mode_turns_off_
automatic_tls` padnie. Test patrzy na wyrenderowana KONFIGURACJE, bo to ona
rozstrzyga, co robi serwer — nie na obecnosc plikow certyfikatow.
"""

import json
import unittest
from pathlib import Path

import jinja2

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "roles" / "mariadb_install" / "templates" / "server.cnf.j2"

BASE_VARS = {
    "galera_cluster_name": "t_galera",
    "galera_nodes_csv": "10.0.0.1,10.0.0.2,10.0.0.3",
    "galera_node_address": "10.0.0.1",
    "gcache_size": "512M",
    "mariadb_tuning": {},
    "lock": {"galera": {"provider_path": "/usr/lib64/galera-4/libgalera_smm.so"}},
}


def render(tls):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, keep_trailing_newline=True)
    # `to_json` wnosi Ansible, nie Jinja — bez niego szablon nie kompiluje sie
    # poza playbookiem i test mierzylby wlasna atrape zamiast produkcyjnego pliku.
    env.filters["to_json"] = json.dumps
    return env.from_string(TEMPLATE.read_text(encoding="utf-8")).render(tls=tls, **BASE_VARS)


class TlsDisabledRenderTests(unittest.TestCase):
    def test_disabled_mode_turns_off_automatic_tls(self):
        """Bez tej linii MariaDB 11.4 szyfruje mimo deklaracji 'disabled'."""
        config = render({"mode": "disabled"})
        server, client = config.split("[client]", 1)
        self.assertIn("disable-ssl", server)
        # Klient 11.4 zada TLS niezaleznie od serwera: bez tego kazde narzedzie
        # lokalne dostaje "SSL is required, but the server does not support it".
        self.assertIn("disable-ssl", client)

    def test_disabled_mode_leaves_no_certificate_paths(self):
        config = render({"mode": "disabled"})
        self.assertNotIn("ssl_ca =", config)
        self.assertNotIn("ssl_cert =", config)
        self.assertNotIn("socket.ssl_", config)
        self.assertNotIn("encrypt=3", config)

    def test_omitted_stanza_behaves_like_disabled(self):
        """Brak sekcji `tls` to ten sam zamiar co 'disabled' — nie cichy TLS."""
        self.assertIn("disable-ssl", render({}))

    def test_full_mode_keeps_encryption_everywhere(self):
        config = render({"mode": "full"})
        self.assertNotIn("disable-ssl", config)
        self.assertIn("ssl_ca =", config)
        self.assertIn("socket.ssl_ca=", config)
        self.assertIn("encrypt=3", config)


if __name__ == "__main__":
    unittest.main()
