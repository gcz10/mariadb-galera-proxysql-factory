# ADR-002: TLS disabled w v1 — udokumentowane ryzyko

**Data:** 2026-07-22
**Status:** Accepted (ZAŁOŻENIE DO POTWIERDZENIA — risk acceptance wymagane w profilu production)
**Decydent:** Principal (Interview 2026-07-22)

## Kontekst

TLS `full` obejmuje: aplikacja→ProxySQL, ProxySQL→MariaDB, Galera replication, IST, SST, monitoring, admin. Wymaga istniejącego PKI/Vault, rotacji certyfikatów i expiry monitoring.

Principal wybrał: `disabled` teraz, `full` zaplanowane w późniejszym feature.

## Decyzja

**`tls.mode: disabled` w v1**, z jawnym ostrzeżeniem i udokumentowanym risk acceptance.

## Uzasadnienie

- Pozwala uruchomić klaster szybciej bez blokady na PKI
- `disabled` nie generuje pustych ścieżek TLS ani restartów TLS
- Wymaga jawnego ostrzeżenia w profilu production (ISC-45)
- Risk acceptance musi być odnotowane w Decisions ISA

## Ryzyko

- Ruch bazy w plaintext w sieci wewnętrznej
- Wymaga zaufanej/izolowanej sieci (administration + database_cluster CIDR)
- Nie spełnia compliance wymagającego szyfrowania w tranzycie

## Konsekwencje

- ISC-44 (TLS full odrzuca niezaufany cert) pozostaje **otwarty** — do implementacji w feature TLS `full`
- ISC-45 (TLS disabled warning + risk acceptance) **aktywne** — F6 generuje ostrzeżenie
- `tls.mode=disabled` w profilu production tworzy jawne ostrzeżenie w deploy report
- Migracja na `full` wymaga osobnego feature z PKI discovery (F0), rotacji i expiry monitoring

## Fog

- fog: Czy istnieje korporacyjny PKI do późniejszego `tls.mode=full`? — do rozstrzygnięcia w F0 discovery
