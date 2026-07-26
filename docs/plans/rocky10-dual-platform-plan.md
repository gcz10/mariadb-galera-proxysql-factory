# Plan: Rocky Linux 10 obok Rocky Linux 9 (dual-platform)

**Status:** PLAN — nie rozpoczęto implementacji.
**Data ustaleń:** 2026-07-26. Wszystkie wersje/URL/sumy **zweryfikowane realnym zapytaniem do repozytoriów**, nie z pamięci.
**Zasada nadrzędna:** kod Rocky 9 **zostaje**. To dodanie drugiej platformy, nie migracja.

---

## 0. Kontekst i konsekwencja do zaakceptowania

Operator kasuje istniejące VM-ki Rocky 9 (`claude-pve`) i stawia nową infrastrukturę na Rocky 10.

**Konsekwencja, którą trzeba świadomie przyjąć:** po skasowaniu VM Rocky 9 **znika możliwość regresji na żywym klastrze EL9**. Kod EL9 pozostaje, ale od tego momentu jest weryfikowalny **wyłącznie statycznie** (schema, syntax-check, lint, walidator inventory). Każda zmiana wspólnego kodu może po cichu zepsuć EL9 i nikt tego nie wychwyci, dopóki ktoś nie odtworzy klastra EL9.

Rekomendacja: traktować `clusters/claude-pve/` jako **zamrożoną referencję konfiguracji EL9** i nie usuwać jej z repo.

---

## 1. Ustalenia — zweryfikowana dostępność EL10

| komponent | EL9 (obecny stan) | EL10 (zweryfikowane) |
|---|---|---|
| Rocky | 9.8, kernel `5.14.0-687.29.1.el9_8` | **10.2** |
| obraz cloud | `Rocky-9.8-GenericCloud.qcow2` | `Rocky-10-GenericCloud-Base-10.2-20260525.0.x86_64.qcow2` |
| MariaDB-server / client / backup | `11.4.12-1.el9` | **`11.4.12-1.el10`** (identyczna wersja) |
| galera-4 | `26.4.27-1.el9` | **`26.4.27-1.el10`** (identyczna) |
| ProxySQL | `3.0.9-1` (centos9) | **`3.0.9-1-centos10`** |
| Keepalived | `2.2.8-6.el9` | `2.2.8-9.el10` |
| python3-PyMySQL | — | `1.1.1-3.el10` |
| socat | `1.7.4.1-8.el9` | `1.7.4.4-8.el10` |
| policycoreutils-python-utils | — | `3.10-1.el10` |
| checkpolicy | — | `3.10-1.el10` |
| lsof / chrony / firewalld / rsync | — | `4.98.0-7.el10` / `4.8-2.el10` / `2.4.0-1.el10_1` / `3.4.1-6.el10_2` |
| **Python** | 3.9 | **3.12.13** |
| node_exporter | 1.12.1 | ta sama statyczna binarka |
| PMM / MinIO / Maildev | 3.8.1 / RELEASE.2025-09-07 / 2.2.1 | kontenery — bez zmian |
| Docker CE | 29.6.2 | repo `centos/10` odpowiada (200) |

**Wniosek: warstwa bazodanowa jest wersyjnie neutralna.** MariaDB i Galera mają na EL10 dokładnie te same wersje, które działają dziś na EL9. To zmiana OS, nie upgrade bazy.

### URL-e i sumy kontrolne (do lockfile EL10)

```
MariaDB repo baseurl (Rocky mapuje sie na "rhel"!):
  https://dlm.mariadb.com/repo/mariadb-server/11.4/yum/rhel/10/x86_64
  (zweryfikowane repomd.xml -> application/xml)

mariadb_repo_setup: BEZ ZMIAN — skrypt jest OS-agnostyczny
  sha256 7325ac7755809ca3312b446bd832542421699298f25b701f9a111bb42df0c7c1
  v2026-06-30, 44184 B

ProxySQL (UWAGA: sciezka "rocky/*" zwraca 404 — trzeba uzyc "centos"):
  https://repo.proxysql.com/ProxySQL/proxysql-3.0.x/centos/10/proxysql-3.0.9-1-centos10.x86_64.rpm
  sha256 4a3e86ef6f96668028398e4841c6d894c3ac058d9a8de0fa60dc9875dc59832e
  40324733 B
  (dla porownania EL9: ca209152e5162aa73474b999e4ab289e89e6dcc2762f12c3018d33a11af1b6da, 40352686 B)
```

---

## 2. Architektura dual-platform — co już działa

**Nie trzeba przebudowywać projektu.** Mechanizm wyboru platformy już istnieje:

- `clusters/<name>/cluster.yml` ma sekcję `versions.lock_file` (schema **wymaga** tego pola).
- Playbooki `f2_install`, `f11_*`, `infra_services` ładują lockfile przez `../{{ versions.lock_file }}`.

Wystarczy nowy lockfile + nowy katalog klastra wskazujący na niego.

**Wyjątek:** cztery miejsca omijają ten mechanizm i mają EL9 wpisane na sztywno. To cała lista zmian w kodzie.

---

## 3. Lista zmian w kodzie (dokładna, 4 miejsca)

| # | plik:linia | obecnie | docelowo |
|---|---|---|---|
| A1 | `playbooks/f2_preflight.yml:13` | `lockfile: "{{ lookup('file', 'versions/versions.lock.yml') \| from_yaml }}"` | ścieżka z `versions.lock_file` |
| A2 | `playbooks/f2_preflight.yml:19-21` | `ansible_distribution_major_version == "9"` + `fail_msg` „Rocky Linux 9" | porównanie z `lockfile.rocky_linux.major`, komunikat generowany |
| A3 | `playbooks/f2_install.yml:13` | `proxysql_repo_baseurl: ".../proxysql-3.0.x/rocky/9/"` | `centos/{{ major }}` z lockfile |
| A4 | `playbooks/f2_install.yml:183` | `...centos/9/proxysql-...-1-centos9.<arch>.rpm` | `centos/{{ major }}/...-1-centos{{ major }}.<arch>.rpm` |

**Pozostałe trafienia `el9` są DANYMI platformy EL9, nie kodem** — zostają nietknięte:
`versions/versions.lock.yml`, `versions/candidate.lock.yml`, `versions/discovered-versions.json`,
`versions/compatibility-report.md`, `clusters/claude-pve/*`.

Dodatkowo: `allowed_minors` musi dopuścić `10.2` w nowym lockfile (preflight to egzekwuje).

---

## 4. Fazy

### R1 — Odhardcodowanie (fundament; wymagane niezależnie od reszty)
Cztery zmiany z §3. Kryterium akceptacji: `cluster-validate` na `clusters/claude-pve/` (EL9) nadal przechodzi statycznie, a playbooki zachowują identyczne zachowanie dla EL9.
Ryzyko: niskie. Bez żywego klastra EL9 weryfikacja tylko statyczna.

### R0 — Lockfile EL10
Nowy `versions/versions-el10.lock.yml` z wartościami z §1. `rocky_linux.major: 10`, `allowed_minors: ["10.2"]`,
`rpm_release: 1.el10`, `galera_provider_version: 26.4.27`, `galera_provider_rpm_release: 1.el10`,
`proxysql.rpm_sha256.x86_64: 4a3e86ef…`, `repo_setup_sha256` bez zmian.
Kryterium: brak placeholderów (`to-confirm-F0`/`to-verify`) — bramka ISC-63.

### R3 — SPIKE: SELinux + SST na jednej VM Rocky 10  ← **NAJPIERW, przed resztą**
**Największa niewiadoma i jedyna bez gotowej odpowiedzi.**
Na EL9 wymagana była **własna kompilacja** modułu z `/usr/share/mariadb/policy/selinux/mariadb-server.te`
(bez niego SST `mariabackup` blokuje się m.in. na `setpgid`). Na EL10 `checkpolicy 3.10` może mieć inną wersję polityki.

Zakres spike'a: 2 VM Rocky 10, MariaDB+galera z repo EL10, kompilacja polityki, **wymuszony realny SST**
(skasować datadir jednego węzła i dołączyć go).
Kryterium akceptacji: węzeł osiąga `Synced` przez SST przy SELinux **enforcing**, `ausearch` bez denials.
Jeśli spike padnie — reszta planu jest bezwartościowa, a koszt naprawy to potencjalnie własny moduł polityki (dni, nie godziny).

### R2 — Terraform pod Rocky 10
Nowy katalog `terraform/<name>-r10/` (**nie modyfikować** `terraform/claude-pve/` — to zniszczyłoby stan EL9).
`source_img` → `Rocky-10-GenericCloud-Base-10.2`. Reszta (sieć, cloud-init, qemu-guest-agent) bez zmian.
Uwaga: `.terraform/`, `*.tfstate*`, `*.tfplan` są w `.gitignore` — pilnować, by nowy katalog też był objęty.

### R4 — Deploy klastra EL10 + weryfikacja
Nowy `clusters/<name>-r10/` (`cluster.yml` + `inventory.yml`), `versions.lock_file: versions/versions-el10.lock.yml`.
Sekwencja: `cluster-validate → cluster-deploy → cluster-bootstrap → cluster-join → cluster-proxysql → cluster-endpoint → cluster-harden → cluster-monitoring → cluster-alerts`.
Bramki: 3/3 `Synced`; dokładnie jeden ONLINE w writer hostgroup; VIP na MASTER;
`mysql_global_status_wsrep_*` widoczne w PMM; 5 reguł alertów w stanie `Normal`.
**Do sprawdzenia w tej fazie:** Python 3.12 vs `ansible.mysql 5.1.0` + `python3-PyMySQL 1.1.1`; firewalld 2.4 i nasz `public.xml`.

### R5 — Backup / restore / chaos na EL10
`cluster-backup` (dynamiczny non-writer) → `cluster-restore-drill` na `rnode1` Rocky 10 →
`chaos-failover`, `chaos-split-brain`, `probe-firewall`.

### R6 — CI: macierz EL9 + EL10
Rozszerzyć job `validate` o walidację obu lockfile'ów i obu zestawów klastrów, żeby zmiana we wspólnym kodzie
nie zepsuła po cichu nieweryfikowalnej już platformy EL9.
**To częściowo rekompensuje utratę żywego klastra EL9.**

---

## 5. Ryzyka

| ryzyko | poziom | uzasadnienie / mitygacja |
|---|---|---|
| SELinux + SST na EL10 | **wysokie** | jedyny element wymagający obejścia na EL9; `checkpolicy` 3.10. Mitygacja: spike R3 **przed** resztą |
| Utrata regresji EL9 | **wysokie** | po skasowaniu VM tylko statyka. Mitygacja: CI matrix (R6) + zamrożona referencja `claude-pve` |
| Python 3.9 → 3.12 | średnie | kolekcje powinny działać, ale brak dowodu z uruchomienia. Weryfikacja w R4 |
| firewalld 2.4 | średnie | wstrzykujemy własny `public.xml`; format stref bywa zmieniany między majorami |
| Dojrzałość buildów MariaDB EL10 | niskie–średnie | wersje identyczne, ale build EL10 młodszy i mniej ostrzelany |
| ProxySQL `centos10` na Rocky 10 | niskie | RPM istnieje; brak oficjalnej ścieżki „rocky" — używamy `centos`, tak jak na EL9 |

---

## 6. Rekomendowana kolejność

```
R1 (odhardcodowanie)  ->  R0 (lockfile EL10)  ->  R3 (SPIKE SELinux/SST)  -- BRAMKA DECYZYJNA --
   -> R2 (terraform)  ->  R4 (deploy+weryfikacja)  ->  R5 (backup/chaos)  ->  R6 (CI matrix)
```

R1 i R0 są tanie i potrzebne niezależnie. **R3 jest bramką** — dopiero jego wynik uzasadnia inwestycję w R2+.

## 7. Otwarte decyzje dla operatora

1. **Nazwa nowego klastra** (np. `claude-r10`) — determinuje `clusters/<name>-r10/`, `terraform/<name>-r10/`, `monitoring.pmm.cluster_name`.
2. **Czy nowy klaster współdzieli PMM** z ewentualnym przyszłym EL9? Jeśli tak — namespacing alertów (`#14`) już to obsługuje; jeśli nie — osobny `infranode`.
3. **Adresacja IP** — czy nowe VM przejmują pulę `192.168.1.10-16`, czy dostają własną (ma znaczenie dla `network.*_cidrs` i VIP).
4. **Czy `#10` (backup SMB)** ma być rozstrzygnięty przy okazji nowej instalacji, czy zostaje S3/MinIO.
