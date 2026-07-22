# Compatibility Report — F1 Version Research

**Research date:** 2026-07-22
**Sources:** mariadb.org, endoflife.date, proxysql.com, rockylinux.org, docs.ansible.com, github.com/ansible-collections/ansible.mysql
**Status:** Read-only research (BLK-4 rozstrzygnięty: internet dostępny). Host-dependent fakty pozostają na F0.

## 1. Rocky Linux 9

| Wartość | Wartość | Źródło |
|---|---|---|
| Major | 9 | rockylinux.org |
| Latest minor | 9.8 (2026-05-27) | rockylinux.org, endoflife.date |
| Major EOL | 2032-05-31 | rockylinux.org |
| Policy | Tylko najnowszy minor wspierany; poprzednie superseded | rockylinux.org |

**Decyzja:** Lockfile przypina `major: 9`, `allowed_minors: [9.8]` (do aktualizacji w F0, gdy hosty mają starszy minor). Hosty muszą być na najnowszym minorze przed deploy (F2 preflight).

## 2. MariaDB

| Seria | Typ | Najnowsza | EOL | Źródło |
|---|---|---|---|---|
| **11.4 (LTS)** | LTS | 11.4.12 (May 2026) | 2029-05 | mariadb.org, endoflife.date |
| 11.8 (LTS) | LTS | 11.8.8 (May 2026) | 2028-06 | mariadb.org |
| 12.3 (LTS) | LTS | 12.3.2 (May 2026) | 2029-06 | mariadb.org |
| 10.11 (LTS) | LTS | 10.11.18 (May 2026) | 2028-02 | mariadb.org |
| ~~10.6~~ | EOL | — | **2026-07-06 EOL** | mariadb.org |
| 10.5 | Rocky9 AppStream | — | starsze | redhat.com |

**Rekomendacja:** **MariaDB 11.4.12 LTS** — najdłuższe wsparcie (May 2029), dojrzały, Galera 4, RPM dla RHEL9 przez oficjalne repo `mariadb_repo_setup --mariadb-server-version=11.4`. Odrzucone warianty:
- 12.3: nowszy, ale krotszy support window i mniejsza dojrzałość
- 11.8: krotszy EOL niż 11.4
- 10.11: starsza seria, krótszy EOL
- 10.5/10.6: EOL lub przestarzałe, niepolecane dla produkcyjnej Galery

**Repo:** `https://downloads.mariadb.com/MariaDB/mariadb_repo_setup` (RHEL9/Rocky9 wykrywany automatycznie). `mariadb-backup` w tym samym repo.

**Galera 4:** wbudowana w pakiety MariaDB Server; instalacja `galera-4` obok `mariadb-server`. wsrep API v26. Rolling upgrades kompatybilne w obrębie 11.x. Od MariaDB 11.4: `plugin-wsrep-provider` dla opcji Galera jako system vars (lepsze niż monolityczny `wsrep_provider_options`).

## 3. ProxySQL

| Wartość | Wartość | Źródło |
|---|---|---|
| Wersja | **3.0.9** (2026-06-05) | proxysql.com |
| Tier | Stable (3.0.x) | proxysql.com |
| Repo | `https://repo.proxysql.com/ProxySQL/proxysql-3.0.x/rocky/9/` | proxysql.com |
| GPG key | `https://repo.proxysql.com/ProxySQL/proxysql-3.0.x/repo_pub_key.gpg` | proxysql.com |

**Security:** 3.0.9 łata CVE-2026-48772 (PROXY Protocol v1 source-IP spoofing → bypass `client_addr` ACL) i CVE-2026-48773 (pre-auth heap overflow MySQL/PostgreSQL). Wersje <3.0.9 wymagają natychmiastowego upgrade.

**Decyzja:** Lockfile przypina ProxySQL 3.0.9. Repo z oficjalnego URL, nie EPEL.

## 4. Ansible

| Wartość | Wartość | Źródło |
|---|---|---|
| ansible-core | **2.21.2** (2026-07-13) | pypi.org, ansible.com |
| ansible.mysql | **5.1.0** | github.com/ansible-collections/ansible.mysql |

**Ważne:** `community.mysql` jest **deprecated** → rename na `ansible.mysql`. FQCN zmienia się z `community.mysql.mysql_query` na `ansible.mysql.mysql_query`. Redirecty działają, ale playbooki muszą używać `ansible.mysql.*`. Planowane usunięcie redirectów w Ansible 17. Dodatkowo `ansible.mariadb` wprowadzona 2026-07 (przyszła dedykowana kolekcja MariaDB); `ansible.mysql` wspiera MariaDB do mid-2027.

**Decyzja:** `requirements.yml` → `ansible.mysql` (nie `community.mysql`). F0 playbook poprawiony.

## 5. Kompatybilność matryca

| Komponent | Wersja | Rocky 9 | Galera 4 | Kompatybilność |
|---|---|---|---|---|
| MariaDB 11.4.12 | LTS | RPM oficjalne repo | Galera 4 wbudowana | Wsparcie do 2029-05 |
| ProxySQL 3.0.9 | Stable | RPM oficjalne repo | `mysql_galera_hostgroups` | Security CVEs patchowane |
| Galera 4 (galera-4) | wsrep API 26 | z MariaDB repo | — | MariaDB 11.x wylacznie |
| ansible-core 2.21.2 | latest | — | — | ansible.mysql 5.1.0 |
| ansible.mysql 5.1.0 | active | — | — | MariaDB wspierana do mid-2027 |

## 6. Blockery pozostające na F0

- F0 musi potwierdzić `rpm -qa` na hostach (co faktycznie zainstalowane)
- F0 musi potwierdzić dokładny RPM release (`dnf info`)
- F0 musi potwierdzić kernel i minor Rocky na hostach
- GPG fingerprinty repozytoriów do weryfikacji w F2 (przed `gpgcheck=1`)

## 7. Odrzucone warianty

- MariaDB 12.3: nowszy ale krotszy EOL, mniejsza dojrzałość LTS
- MariaDB 10.11/11.8: krotszy EOL niż 11.4
- MariaDB 10.5/10.6: EOL lub przestarzałe
- ProxySQL <3.0.9: krytyczne CVE
- community.mysql: deprecated
- EPEL dla ProxySQL: przestarzałe, brak security fixes
