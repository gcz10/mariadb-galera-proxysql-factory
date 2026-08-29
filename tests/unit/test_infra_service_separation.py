#!/usr/bin/env python3
"""Monitoring, magazyn kopii i relay maja rozne cykle zycia.

POWSTAL PO REALNEJ STRACIE (2026-08-24). PMM, MinIO i Maildev byly jednym,
nierozdzielnym zestawem na hoscie `infra`. Przebudowa monitoringu oznaczala
zniszczenie hosta, a wraz z nim magazynu kopii CALEJ floty — mimo ze magazyn
mial zostac nietkniety i moze rownie dobrze stac poza laboratorium.

Kontrakt: sklad uslug jest deklaracja platformy (`platform.infra.services`),
a szablon compose renderuje WYLACZNIE to, co zadeklarowano — razem z wolumenami
i sekretami. Brak deklaracji zachowuje sie jak dawniej (pelny zestaw).
"""
import json
import unittest
from pathlib import Path

import yaml
from jinja2 import Template

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "roles" / "infra_services" / "templates" / "compose.yml.j2"
PLAYBOOK = REPO / "playbooks" / "infra_services.yml"
SCHEMA = REPO / "platform" / "schema" / "platform.schema.json"

LOCK = {
    "pmm": {"image": "percona/pmm-server:3.9.1", "image_digest": "sha256:" + "a" * 64},
    "minio": {"image": "minio/minio:RELEASE", "image_digest": "sha256:" + "b" * 64},
    "maildev": {"image": "maildev/maildev:2.2.1"},
}


def render(services):
    tmpl = Template(COMPOSE.read_text(encoding="utf-8"))
    # `to_json` dostarcza Ansible, nie Jinja — bez niego szablon nie renderuje sie
    # poza playbookiem, a kontrakt ma sprawdzac dokladnie ten plik, ktory idzie na host.
    tmpl.environment.filters["to_json"] = json.dumps
    return yaml.safe_load(
        tmpl.render(
            infra_services=services,
            lock=LOCK,
            platform={"name": "t"},
            ansible_host="10.0.0.1",
            pmm_admin_password="haslo-admina",
            minio_root_user="root-user",
            minio_root_password="haslo-minio",
        )
    )


class InfraServiceSeparationTests(unittest.TestCase):
    def test_full_set_renders_all_three(self):
        out = render(["pmm", "minio", "maildev"])
        self.assertEqual(sorted(out["services"]), ["maildev", "minio", "pmm-server"])
        self.assertEqual(sorted(out["volumes"]), ["minio-data", "pmm-data"])

    def test_monitoring_only_leaves_no_storage_behind(self):
        """Najwazniejszy przypadek: PMM bez MinIO."""
        out = render(["pmm", "maildev"])
        self.assertNotIn("minio", out["services"])
        self.assertNotIn("minio-data", out["volumes"], "wolumen magazynu nie moze powstac")
        rendered = json.dumps(out)
        self.assertNotIn("MINIO_ROOT", rendered, "sekret magazynu nie moze trafic do compose")

    def test_storage_only_leaves_no_monitoring_behind(self):
        out = render(["minio"])
        self.assertEqual(list(out["services"]), ["minio"])
        self.assertNotIn("pmm-data", out["volumes"])

    def test_pmm_without_relay_disables_smtp(self):
        """Bez relaya alert nie ma dokad wyjsc — konfiguracja musi to mowic wprost."""
        out = render(["pmm"])
        env = out["services"]["pmm-server"]["environment"]
        self.assertEqual(env["GF_SMTP_ENABLED"], "false")
        self.assertNotIn("depends_on", out["services"]["pmm-server"])

    def test_playbook_defaults_to_full_set(self):
        """Platformy sprzed tej zmiany nie deklaruja nic i maja dzialac jak dawniej."""
        text = PLAYBOOK.read_text(encoding="utf-8")
        self.assertIn("platform.infra.services | default(['pmm', 'minio', 'maildev'])", text)

    def test_secret_assertions_follow_the_declaration(self):
        """Warstwa bez MinIO nie moze wymagac sekretow MinIO."""
        text = PLAYBOOK.read_text(encoding="utf-8")
        self.assertIn("'minio' not in infra_services or minio_root_user", text)
        self.assertIn("'pmm' not in infra_services or pmm_admin_password", text)
    def test_service_play_loads_shared_ingress_matrix(self):
        """Drugi play infra musi ladowac vars_files we wlasnym zakresie."""
        plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
        service_play = next(
            play for play in plays if play["name"] == "Infra — uslugi warstwy wspolnej zadeklarowane przez platforme"
        )
        self.assertIn("vars/infra_ingress.yml", service_play.get("vars_files", []))

    def test_schema_constrains_the_service_names(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        services = schema["properties"]["platform"]["properties"]["infra"]["properties"]["services"]
        self.assertEqual(sorted(services["items"]["enum"]), ["maildev", "minio", "pmm"])
        self.assertTrue(services["uniqueItems"])


if __name__ == "__main__":
    unittest.main()
