# Profile środowiskowe

`cluster.environment: production` i `platform.environment: production`
automatycznie włączają wymagany `production.yml`. Nie ma pola `cluster.profile`,
przełącznika omijającego profil ani wyjątku akceptacji ryzyka.

## Kontrakt produkcyjny

- `versions.policy: locked`; pakiety i obrazy opisuje wskazany lockfile.
- MariaDB: `tls.mode: full`, bez `require_secure_transport: false`;
  `innodb_flush_log_at_trx_commit` równe `1` lub `"1"`. Pominięcie zachowuje
  bezpieczną wartość szablonu `1`. Stare `durability_risk_accepted` jest odrzucane.
- PMM: HTTPS, `validate_certs: true`, jawne `ca_reference`, bez loopbacku
  i poświadczeń w URL. Wersja serwera musi odpowiadać lockfile; produkcja
  nie narzuca operatorowi dystrybucji Docker.
- Backup: włączony i szyfrowany; zdalny S3 z `secure: true` albo SMB z `seal`.
  Lokalny `filesystem` nie dowodzi położenia poza hostem i jest odrzucany.
  Walidacja sprawdza nazwy/adresy Galery; preflight na każdym węźle rozwiązuje
  także DNS i porównuje wynik z adresami źródeł oraz lokalnych interfejsów.
  Brak pomiaru/DNS blokuje deployment. To nie jest dowód osobnej lokalizacji
  geograficznej, niezależnego zasilania ani niezależności hypervisora.
- `backup.restore_test_schedule: disabled`. Drille wymagają osobnego,
  izolowanego środowiska nieprodukcyjnego. Wyłączony drill nie wymaga metryki
  świeżości restore ani reguły `restore-drill-stale`; backup i TLS nadal są mierzone.
- `monitoring.alerts.email`: poprawny adres, nie domena laboratoryjna
  (`.invalid`, `.test`, `.example`, `.localhost`, `.local`, `localhost`
  ani `example.com/net/org` i ich subdomeny).
  To walidacja deklaracji odbiorcy, nie dowód doręczenia wiadomości.
- Platforma: jawne `platform.infra.services: []`, brak hostów `infra`, wymagany
  host `app` do pomiaru TLS frontendu. PMM, SMTP i magazyn kopii prowadzi
  operator poza laboratoryjnym stackiem. Zarządzane artefakty `isa-infra`
  na hostach platformy blokują preflight; nic nie jest automatycznie usuwane.
  Backup wolumenu PMM należy do operatora zewnętrznej usługi.

## Budowa i pomiar

Po dostarczeniu rzeczywistych hostów, PKI, usług i sekretów używa się tych samych
celów operatora:

```bash
make platform-build PLATFORM=<nazwa>
make cluster-build CLUSTER=<nazwa> CONFIRM=yes
make cluster-production-verify CLUSTER=<nazwa>
```

`platform-build` pomija laboratoryjne `platform-infra` i `platform-pmm-backup`.
Bezpośrednie `platform-infra` z `services: []` daje jawny, kontrolerowy no-op;
żądanie usługi laboratoryjnej jest błędem także przy pustej grupie `infra`.

`cluster-build` nie zasiewa danych, nie uruchamia restore-drill ani generatora
obciążenia. Backup, konfiguracja hosta aplikacyjnego i alerty są obowiązkowe;
`BUILD_SKIP` dopuszcza tylko inertny token `seed`. Bramka produkcyjna mierzy
Galera/ProxySQL, endpoint TLS, hardening, SELinux, firewall, integralność
i świeżość backupu oraz PMM bez ponownego converge i DDL pomiarowego.
`lab-post-build-gate` odmawia produkcji.

Walidatory YAML i preflight Ansible używają tego samego profilu. Preflight
sprawdza efektywne zmienne po `-e` w kontekście każdego wybranego hosta;
`--limit` nie pomija tej kontroli. Odrzuca także obniżenie środowiska
deklarowanego jako production. Produkcyjne sondy PMM/S3 nie pobierają sekretów
z `tests/lab/.env` ani konta root MinIO.
Zmienne pomocnicze Make nie pozwalają nadpisać środowiska wybranego z deklaracji
i wejść przez to do laboratoryjnej bramki lub backupu wolumenu PMM.

## Granica dowodu

Lokalne testy i smoke kontrolera dowodzą polityki, odmów i orkiestracji,
nie wdrożenia na rzeczywistej produkcji. Uruchomienie wymaga prawdziwych adresów,
znanych kluczy SSH, materiału PKI, przypiętego PMM z działającym SMTP,
zdalnego magazynu oraz poświadczeń operatora. Doręczenie alertu, odtwarzalność
produkcyjnych danych, RTO/RPO i pojemność gcache wymagają osobnych pomiarów;
bramka gotowości ich nie zastępuje.
