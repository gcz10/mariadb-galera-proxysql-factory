# Pełny rebuild EL9 — xenonv11 + orionv12-r9 + cassiopeiav11-r9 (2026-08-29)

## Decyzja i granica zakresu

Wykonano clean-room rebuild warstwy platformowej i obu aktywnych klastrów na
Rocky Linux 9. Wyjątek jest jawny: `x10mon` (VMID `10000`,
`192.168.1.160`) pozostał bez zmian, ponieważ jest właścicielem zachowanego
MinIO, PMM i Maildev.

Usunięto stare definicje v10 i przeniesiono je do
`docs/records/archives/`. Definicje v8/v9 nadal pozostają w repo jako
zatrzymane środowiska historyczne.

## Ochrona danych i teardown

- Przed teardownem skopiowano dane MinIO na kontroler do
  `~/minio-archive-20260829`: 45 obiektów, `722M` zajętego miejsca.
- Oryginalny MinIO na `x10mon:9000` nie był niszczony. Po rebuildzie kontener
  `minio` działa razem z `pmm-server` i `maildev`.
- Usunięto 11 starych VM: `x10p1`, `x10p2`, `x10app`, `o11db1-3`, `o11r1`,
  `c10db1-3`, `c10r1` (VMID `10001-10011`). Teardown zakończył się bez sierot
  ZFS.
- `x10mon` pozostał uruchomiony; jego dane MinIO były dostępne podczas
  provisioningu nowych klastrów.

## Mapa nowej floty

| Warstwa | Host | VMID | IP | OS / rola |
|---|---|---:|---|---|
| `xenonv11` | `x11p1` | 10001 | 192.168.1.161 | Rocky 9.8 / ProxySQL |
| `xenonv11` | `x11p2` | 10002 | 192.168.1.162 | Rocky 9.8 / ProxySQL |
| `xenonv11` | `x11app` | 10003 | 192.168.1.163 | Rocky 9.8 / aplikacja |
| `orionv12-r9` | `o12db1` | 10004 | 192.168.1.164 | Rocky 9.8 / Galera |
| `orionv12-r9` | `o12db2` | 10005 | 192.168.1.165 | Rocky 9.8 / Galera |
| `orionv12-r9` | `o12db3` | 10006 | 192.168.1.166 | Rocky 9.8 / Galera |
| `orionv12-r9` | `o12r1` | 10007 | 192.168.1.167 | Rocky 9.8 / restore |
| `cassiopeiav11-r9` | `c11db1` | 10008 | 192.168.1.168 | Rocky 9.8 / Galera |
| `cassiopeiav11-r9` | `c11db2` | 10009 | 192.168.1.169 | Rocky 9.8 / Galera |
| `cassiopeiav11-r9` | `c11db3` | 10010 | 192.168.1.170 | Rocky 9.8 / Galera |
| `cassiopeiav11-r9` | `c11r1` | 10011 | 192.168.1.171 | Rocky 9.8 / restore |
| zachowana infrastruktura | `x10mon` | 10000 | 192.168.1.160 | Rocky 10.2 / MinIO + PMM + Maildev |

Wspólny endpoint to `192.168.1.172:6033`, z VIP-em na `x11p1`. Nowa
platforma nie deklaruje własnej grupy `infra`: monitoring i S3 są świadomie
obsługiwane przez zachowany `x10mon`, który jest dodany do inwentarzy tenantów.

## Artefakty i wersje

- `platform/xenonv11/` — platforma Rocky 9, dwa ProxySQL, aplikacyjny
  `x11app`, VIP `.172`.
- `clusters/orionv12-r9/` — MariaDB `11.8.9`, Galera `26.4.27`, hostgroupy
  `610/620/630/640`, użytkownik `app_user_ov12`, bucket
  `orionv12-galera-backups`.
- `clusters/cassiopeiav11-r9/` — MariaDB `11.8.9`, Galera `26.4.27`,
  hostgroupy `650/660/670/680`, użytkownik `app_user_cv11`, bucket
  `cassiopeiav11-galera-backups`.
- Oba klastry używają `versions/versions-el9-118.lock.yml` i osobnych PKI
  (`pki/o12`, `pki/c11`); frontend ProxySQL korzysta z CA `pki/xenonv11`.
- `xenonv11` korzysta z `pki/xenonv11`; nowe certyfikaty nie zostały
  odziedziczone po v10.
- `mariadb_tuning.gcache_size` wynosi `512M` w obu nowych klastrach.

## Sekwencja wykonania

1. Archiwizacja MinIO na kontroler i weryfikacja zachowanego endpointu.
2. Teardown starych VM `10001-10011`; `x10mon` wyłączony z destrukcyjnego
   zakresu.
3. Provision 11 nowych VM Rocky 9.8 oraz `cluster-trust-hosts` (8/8 kluczy
   dla każdego tenanta; platforma 3/3).
4. `platform-build PLATFORM=xenonv11` oraz `platform-verify`.
5. Usunięcie osieroconych rejestracji starych węzłów z PMM na `x10mon`.
6. `cluster-build CLUSTER=orionv12-r9 CONFIRM=yes` i
   `cluster-build CLUSTER=cassiopeiav11-r9 CONFIRM=yes` z pełnymi bramkami.

Pierwsze przejście bramki po budowie wykryło prawdziwy dryf: `site.yml` i
`f5_join.yml` zapisywały ten sam plik `/etc/tmpfiles.d/mariadb.conf`, ale z
różnymi komentarzami. Kopia z `f5_join.yml` zmieniała checksumę przy każdym
joinie, a kolejny converge zgłaszał `changed=2`. Zrównano treść obu zadań;
ponowienie joinu i pełne `cluster-build` obu tenantów zakończyło się PASS.

Ponieważ zachowany `x10mon` nie jest hostem zarządzanym przez `platform-build`,
`probe-proxysql-tenancy.py` porównuje grupę `infra` tylko wtedy, gdy platforma
rzeczywiście ją deklaruje; nadal porównuje zawsze zarządzane kopie `proxysql`
i `app`. Dzięki temu zewnętrzny host backupów nie jest fałszywie raportowany
jako dryf inventory.

## Dowody akceptacji zmierzone po rebuildzie

- `make platform-verify PLATFORM=xenonv11`: PASS — 2 ProxySQL, VIP `.172`
  na `x11p1`, TLS endpointu z hosta aplikacyjnego, PMM bez zależności od
  Galery.
- Pełne post-build gates obu klastrów: PASS — idempotence, Galera, ProxySQL,
  endpoint, hardening, app-conformance, backup, restore drill,
  rolling-restart, upgrade-plan, patch, drift, gcache i PMM-native.
- `cluster-health`: oba klastry mają na każdym węźle
  `wsrep_local_state=4`, `wsrep_cluster_status=Primary`,
  `wsrep_cluster_size=3`, `wsrep_connected=ON`, `wsrep_ready=ON`.
- ProxySQL: jeden aktywny writer, trzy zdrowe backendy, `runtime==disk`, brak
  query rules, domyślne konto administracyjne odrzucone.
- TLS aplikacyjny: `TLS_AES_256_GCM_SHA384`, read-your-writes,
  `ROLLBACK`/`COMMIT`, jeden writer; certyfikaty klastra i VIP zweryfikowane.
- Orion: `galera-orionv12-r9-20260829-230007` w S3, AES-256-CBC, sha256 OK,
  metadata MariaDB `11.8.9`, `seqno=0`; restore drill zweryfikował 503 wiersze.
- Cassiopeia: `galera-cassiopeiav11-r9-20260829-231058` w S3, AES-256-CBC,
  sha256 OK, metadata MariaDB `11.8.9`, `seqno=0`; restore drill
  zweryfikował 503 wiersze.
- Gcache: zmierzony write rate `74222 B/s`, wymagane minimum `128M`, wdrożone
  `512M` w obu klastrach.
- PMM: wersja `3.9.1`, po trzy namespaced nodes, trzy node-exportery
  `1.12.1`, trzy usługi MySQL na tenant, QAN, metryki Galera/freshness/lifecycle
  i reguły ISC-47.
- Zachowany `x10mon`: Rocky `10.2`, Docker `29.7.2`; kontenery `minio`,
  `pmm-server` (healthy) i `maildev` działają.
- `make fleet-state`: 12 VM w stanie `running` — 11 nowych VM EL9 oraz
  zachowany `x10mon`; nowe definicje odpowiadają aktywnej platformie i obu
  aktywnym klastrom.

## Stan po kampanii

Aktywne są `xenonv11`, `orionv12-r9` i `cassiopeiav11-r9`. Endpoint `.172`
odpowiada. Platformy i klastry v8/v9 pozostają zatrzymane jako historyczne
środowiska; stare v10 zostały zarchiwizowane po teardownie. MinIO i jego
istniejące dane pozostały na `x10mon`.
