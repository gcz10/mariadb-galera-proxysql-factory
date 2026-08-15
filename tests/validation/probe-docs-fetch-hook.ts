#!/usr/bin/env -S deno run --allow-read
// Sonda dla `.omp/hooks/pre/docs-fetch.ts`.
//
// Korpus testowy to PRAWDZIWE polecenia z sesji 2026-08-15: te trzy, ktore
// zeskrobaly `mariadb.org` zamiast uzyc `read`, oraz te, ktore w tym samym
// przebiegu legalnie odpytaly API, rejestr obrazow i repozytorium pakietow.
//
// Falszywy alarm jest tu grozniejszy niz przeoczenie: bramka, ktora blokuje
// `curl` do Docker Huba, zostanie wylaczona w tydzien i nie ochroni przed
// niczym. Dlatego lista ALLOW jest dluzsza od listy BLOCK.
//
// Uruchomienie: deno run --allow-read tests/validation/probe-docs-fetch-hook.ts

import { inspectCommand } from "../../.omp/hooks/pre/docs-fetch.ts";

/** Realne wpadki — kazde z nich naprawde padlo w sesji. */
const MUST_BLOCK: Record<string, string> = {
  "scrape mariadb.org/about przez regex po tagach":
    `curl -sL "https://mariadb.org/about/" | python3 -c "` +
    `import sys,re,html; t=re.sub(r'<[^>]+>',' ',sys.stdin.read())"`,
  "scrape podstrony polityki utrzymania":
    `curl -sL "https://mariadb.org/about/maintenance-policy/" | python3 -c "` +
    `import sys,re; rows=re.findall(r'<tr>(.*?)</tr>', sys.stdin.read(), re.S)"`,
  "scrape po kotwicy, wyciaganie hrefow":
    `curl -sL "https://mariadb.org/about/" | python3 -c "` +
    `import sys,re; [print(m.group(1)) for m in re.finditer(r'href=\\"([^\\"]+)\\"', sys.stdin.read())]"`,
  "release notes produktu przez curl zamiast read":
    `curl -sL https://docs.percona.com/percona-monitoring-and-management/3/release-notes/3.9.0.html`,
  "dokumentacja Ansible przez curl":
    `curl -s https://docs.ansible.com/ansible/latest/collections/index.html | head -50`,
};

/** Realne, poprawne wywolania z tej samej sesji. Zaden nie moze zostac zablokowany. */
const MUST_ALLOW: Record<string, string> = {
  "REST API MariaDB (mimo hosta z rodziny mariadb)":
    `curl -s "https://downloads.mariadb.org/rest-api/mariadb/11.4/"`,
  "manifest w rejestrze Docker Hub":
    `curl -sI -H "Authorization: Bearer $TOK" "https://registry-1.docker.io/v2/percona/pmm-server/manifests/3.9.0"`,
  "token do rejestru":
    `curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:percona/pmm-server:pull"`,
  "listing RPM-ow w repo Percony (grep po HTML, ale to nie dokumentacja)":
    `curl -s "https://repo.percona.com/pmm3-client/yum/release/9/RPMS/x86_64/" | grep -oE 'pmm-client-3\\.9\\.0[^"]*\\.rpm'`,
  "API PMM na hoscie w labie":
    `curl -sSk -u admin:$PW https://192.168.1.130/v1/inventory/nodes`,
  "usuwanie uslugi przez API PMM":
    `curl -sSk -X DELETE -u "$AUTH" "https://192.168.1.130/v1/inventory/services/abc?force=true"`,
  "tagi z API GitHuba":
    `curl -sL -H "User-Agent: curl" "https://api.github.com/repos/codership/galera/tags?per_page=6"`,
  "strona wydania na GitHubie (nie jest hostem dokumentacji)":
    `curl -sL -o /tmp/pg -w '%{http_code}' "https://github.com/sysown/proxysql/releases/tag/v3.0.9"`,
  "VictoriaMetrics w PMM":
    `curl -sSk -u admin:$PW "https://192.168.1.130/victoriametrics/api/v1/query?query=mysql_up"`,
  "polecenie bez pobierania czegokolwiek":
    `ansible-playbook playbooks/site.yml -i clusters/newclaude3-r9/inventory.yml`,
  "make bez sieci":
    `make cluster-monitoring CLUSTER=newclaude3-r9`,
  "curl do lokalnego dashboardu":
    `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3847/`,
  // Ponizsze dwa egzekwuja MACHINE_EXT i MACHINE_PATH. Bez nich obie reguly
  // sa martwe — sprawdzone mutacja: ich usuniecie nie zmienialo wyniku sondy.
  // Wzorzec jest realny: artefakt maszynowy serwowany z hosta dokumentacji.
  "spec OpenAPI pod hostem dokumentacji (rozszerzenie .json wygrywa)":
    `curl -sL https://docs.percona.com/percona-monitoring-and-management/3/api/swagger.json`,
  "sciezka API pod hostem sklasyfikowanym jako dokumentacja":
    `curl -s "https://mariadb.com/rest-api/mariadb/11.4/"`,
};

let failures = 0;

for (const [label, command] of Object.entries(MUST_BLOCK)) {
  const verdict = inspectCommand(command);
  if (!verdict.block) {
    console.log(`FAIL: powinno byc ZABLOKOWANE, przeszlo — ${label}`);
    failures++;
  }
}

for (const [label, command] of Object.entries(MUST_ALLOW)) {
  const verdict = inspectCommand(command);
  if (verdict.block) {
    console.log(`FAIL: falszywy alarm na ${verdict.url} — ${label}`);
    failures++;
  }
}

const total = Object.keys(MUST_BLOCK).length + Object.keys(MUST_ALLOW).length;
if (failures > 0) {
  console.log(`FAIL: docs-fetch hook — ${failures}/${total} przypadkow zle`);
  // `process.exitCode`, nie `Deno.exit`: sonda musi zwrocic 1 zarowno pod
  // `deno`, jak i pod `node` (CI ma node, nie ma deno). `Deno.exit` wywalalby
  // sie pod node'em z ReferenceError — czyli sciezka PORAZKI bylaby zepsuta,
  // a tego sonda z definicji nie moze miec.
  process.exitCode = 1;
} else {
  console.log(
    `PASS: docs-fetch hook — ${Object.keys(MUST_BLOCK).length} blokad, ` +
      `${Object.keys(MUST_ALLOW).length} przepustek, 0 falszywych alarmow`,
  );
}
