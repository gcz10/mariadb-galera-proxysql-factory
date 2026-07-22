# ADR-004: MariaDB 11.4.12 LTS — wybór wersji

**Data:** 2026-07-22
**Status:** Accepted (do potwierdzenia w F0 — co jest na hostach)
**Decydent:** F1 research (oficjalne źródła)

## Kontekst

MASTER_PROMPT §4 wymaga wyboru wersji na podstawie wsparcia, zgodności, pakietów, security fixes i testu integracyjnego — nie najwyższego numeru. Hierarchia dowodów: oficjalna dokumentacja > release notes > wiedza modelu jako hipoteza.

## Kandydaci (F1 research, 2026-07-22)

| Seria | Typ | Najnowsza | EOL | Źródło |
|---|---|---|---|---|
| **11.4** | LTS | 11.4.12 (May 2026) | 2029-05 | mariadb.org, endoflife.date |
| 11.8 | LTS | 11.8.8 (May 2026) | 2028-06 | mariadb.org |
| 12.3 | LTS | 12.3.2 (May 2026) | 2029-06 | mariadb.org |
| 10.11 | LTS | 10.11.18 (May 2026) | 2028-02 | mariadb.org |
| ~~10.6~~ | EOL | — | 2026-07-06 | mariadb.org |
| 10.5 | Rocky9 AppStream | — | starsze | redhat.com |

## Decyzja

**MariaDB 11.4.12 LTS.**

## Uzasadnienie

- Najdłuższe wsparcie (EOL 2029-05) — 3 lata okna
- Galera 4 wbudowana (wsrep API 26) — jedyny wspierany provider dla 11.x
- RPM dla RHEL9/Rocky9 przez oficjalne repo (`mariadb_repo_setup --mariadb-server-version=11.4`)
- `mariadb-backup` w tym samym repo (SST + backup)
- Od 11.4: `plugin-wsrep-provider` dla opcji Galera jako system vars
- Dojrzały LTS (GA 2024-05, 2 lata stabilności)

## Odrzucone warianty

- **12.3:** nowszy LTS, ale krotszy support window i mniejsza dojrzałość
- **11.8:** krotszy EOL (2028-06) niż 11.4 (2029-05)
- **10.11:** starsza seria, krótszy EOL (2028-02)
- **10.6:** EOL 2026-07-06 — brak security updates
- **10.5:** Rocky9 AppStream default — przestarzały dla Galery

## Warunek

- F0 musi potwierdzić `rpm -qa` na hostach (co faktycznie zainstalowane)
- F0 musi potwierdzić dokładny RPM release (`dnf info`)
- Jeśli hosty mają starszą wersję, F2 (preflight) wymaga upgrade przed deploy
- GPG fingerprint repozytorium do weryfikacji w F2

## Źródła

- https://mariadb.org/download/
- https://endoflife.date/mariadb
- https://downloads.mariadb.com/MariaDB/mariadb_repo_setup
