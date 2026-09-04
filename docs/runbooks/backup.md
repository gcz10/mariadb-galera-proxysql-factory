# Runbook: backup i restore Galera

**Status:** aktualny  
**Powiązane ISC:** ISC-32, ISC-33, ISC-34, ISC-35, ISC-36, ISC-38, ISC-39

## Kontrakt

`galera-backup` wykonuje pełny fizyczny backup przez `mariadb-backup`, a potem
szyfruje archiwum strumieniowo przez `python3-cryptography` w formacie 3:
`GB3G | salt | nonce | ciphertext | tag`. Nagłówek jest AAD; odszyfrowany plik
jest publikowany atomowo dopiero po poprawnej weryfikacji tagu GCM. Pamięć
pozostaje ograniczona rozmiarem fragmentu, niezależnie od rozmiaru backupu.
Czytniki formatu 2 (`GB2G`, wcześniejszy GCM one-shot) i formatu 1 (`openssl
aes-256-cbc` + PBKDF2) pozostają dostępne dla istniejących kopii. Klucz bez
zmian: `GALERA_BACKUP_ENCRYPTION_KEY`. Kontrakt `Cipher`/GCM:
https://github.com/pyca/cryptography/blob/main/docs/hazmat/primitives/symmetric-encryption.rst

Runner publikuje checksumę i metadata, a następnie usuwa lokalny staging.
Obsługiwane backendy:

- `s3` — bucket S3/MinIO;
- `smb` — udział montowany tylko na czas operacji; sukces jest zapisywany dopiero po poprawnym unmount;
- `filesystem` — zasób wcześniej zamontowany przez operatora; runner nigdy go nie montuje ani nie odmontowuje.

Źródłem backupu jest dokładnie `backup.scheduler.host`. Runner nie wybiera automatycznie „non-writera”; wybierz zdrowy węzeł Galery o akceptowalnym wpływie I/O. Przed rozpoczęciem sprawdza `Primary`, `Synced`, `wsrep_ready=ON`, `wsrep_connected=ON` i oczekiwany rozmiar klastra.

Backup pełny jest zaimplementowany. `incremental_backup_schedule` musi mieć wartość `disabled`.

## Konfiguracja klastra

Każdy klaster ma dokładnie jeden blok backendu. Wspólne pola:

```yaml
backup:
  enabled: true
  destination: s3
  full_backup_schedule: "0 2 * * *"
  incremental_backup_schedule: "disabled"
  freshness_sla_hours: 26
  retention_days: 14
  encryption_enabled: true
  immutable_or_offsite_copy: false
  restore_test_schedule: "0 4 * * 0"
  scheduler:
    mode: cron
    host: gnode1
    timezone: UTC
  s3:
    endpoint: "192.0.2.40:9000"
    bucket: "cluster-a-backups"
    secure: true
```

`scheduler.mode: cron` instaluje `/etc/cron.d/galera-backup-<cluster>` wyłącznie na `scheduler.host`. `manual` nie instaluje crona. `freshness_sla_hours` jest niezależnym od retencji progiem alarmowym ostatniego udanego backupu; dla harmonogramu dziennego wartość `26` daje dwie godziny tolerancji. `restore_test_schedule` opisuje oczekiwaną częstotliwość drill; repozytorium nie uruchamia automatycznego crona restore.

SMB zastępuje blok `s3`:

```yaml
  destination: smb
  smb:
    source: "//backup.example.net/galera"
    mount_point: "/mnt/galera-backup"
    options:
      - "vers=3.1.1"
      - "seal"
      - "nosuid"
      - "nodev"
      - "noexec"
```

`source` musi wskazywać dokładnie jeden udział `//server/share`. Opcje są małymi literami; wymagane są SMB 3.x, `seal`, `nosuid`, `nodev` i `noexec`. Runner sprawdza moduł CIFS dla aktualnie uruchomionego kernela. Gdy moduł istnieje tylko dla nowszego zainstalowanego kernela, operacja kończy się diagnostyką wymagającą zaplanowanego rebootu — nie rebootuje hosta.

Wcześniej zamontowany filesystem zastępuje blok backendu:

```yaml
  destination: filesystem
  filesystem:
    mount_point: "/mnt/galera-backup"
    expected_fstype: "nfs4"
```

Mount musi istnieć przed uruchomieniem. Runner sprawdza target, source i typ filesystemu przed publikacją, po publikacji i przy cleanupie. Zmiana lub utrata mountu blokuje sukces.

Walidacja:

```bash
make cluster-validate CLUSTER=<name>
python3 tests/validation/validate-backup-config.py clusters
```

Walidator odrzuca mieszane backendy, niekanoniczny UNC, relatywny mount point, słabe opcje SMB, nieznany scheduler i niezabezpieczony S3 w profilu `production`.

## Sekrety

Sekrety przekazuj przez środowisko kontrolera albo ignorowany plik `.env`, nigdy przez repozytorium lub dodatkowe argumenty procesu:

```bash
export GALERA_BACKUP_ENCRYPTION_KEY='<długi-losowy-klucz>'

# Zewnętrzny S3:
export GALERA_BACKUP_S3_ACCESS_KEY='<scoped-access-key>'
export GALERA_BACKUP_S3_SECRET_KEY='<scoped-secret-key>'

# SMB:
export GALERA_BACKUP_SMB_USERNAME='<konto-usługi>'
export GALERA_BACKUP_SMB_PASSWORD='<hasło>'
export GALERA_BACKUP_SMB_DOMAIN='<domena-opcjonalna>'
```

Rola zapisuje wyłącznie wymagane wartości w:

```text
/opt/galera-backup/clusters/<cluster>/secrets.env
```

Plik ma właściciela `root:root` i tryb `0600`. Para scoped credentials z prawem **zapisu** trafia na każdy węzeł Galery (donora wybiera runner przy starcie) oraz na host restore. Osobna para z prawem **kasowania** trafia wyłącznie na `backup.scheduler.host` — patrz „Rozdział poświadczeń" niżej. Hasło SMB jest przekazywane do `mount.cifs` przez tymczasowy plik credentials `0600`, nigdy jako `password=...` w argv. Plik jest usuwany także po błędzie mountu lub unmountu.

Dla zewnętrznego S3 administrator storage musi przed przekazaniem scoped credentials utworzyć w buckecie `galera-backup-owner.json` o treści `{"format_version":1,"cluster_name":"<cluster>"}`. Konto runnera dostaje tylko `GetObject` do markera oraz operacje na prefiksie `galera-<cluster>-*`; nie może zmienić ani usunąć markera.

### Rozdział poświadczeń: zapis kontra kasowanie

Runner stoi na wszystkich węzłach Galery, więc poświadczenie zapisu leży na każdym z nich. Gdyby miało `s3:DeleteObject`, kompromitacja dowolnego węzła bazy kasowałaby historię kopii off-cluster. Dlatego są dwa konta:

| Konto | Gdzie leży | Uprawnienia |
|---|---|---|
| `galera-backup-<cluster>` | każdy węzeł Galery + host restore | `GetObject`, `PutObject`, `AbortMultipartUpload`, `ListMultipartUploadParts` na `galera-<cluster>-*`; **bez `Delete*`** |
| `galera-backup-prune-<cluster>` (skracane do 32 znaków — patrz niżej) | tylko `backup.scheduler.host` | `ListBucket`, `GetObject`, `DeleteObject` na `galera-<cluster>-*`; **bez `PutObject`** |

Nazwy kont retencyjnych dłuższe niż 32 znaki (limit akceptowany przez MinIO)
są skracane deterministycznie: pierwsze 19 znaków pełnej nazwy (dla tego
prefiksu: `galera-backup-prune`, bez końcowego łącznika), potem `-` i
12-znakowy skrót sha256 pełnej nazwy. Dokładną nazwę dla swojego klastra
obliczy filtr `minio_service_account_name`
(`roles/galera_backup/filter_plugins/minio_access_keys.py`); ta sama funkcja
nadaje ją przy provision i odnajduje przy derejestracji.

Retencja (`run_retention`) biegnie na koordynatorze — także wtedy, gdy backup wykonał inny węzeł. Węzeł bez poświadczenia retencji nie emituje zdarzeń retencji; to normalny stan, nie awaria. Zdarzenie `retention.success` w `events.jsonl` na koordynatorze niesie liczbę usuniętych kopii.

**Skutek operacyjny:** gdy koordynator jest długo niedostępny, kopie nadal powstają (inny węzeł zostaje donorem), ale wygasłe przestają być kasowane do jego powrotu. Bucket rośnie; świeżość kopii pozostaje nienaruszona.

**Ryzyko rezydualne:** klucz zapisu może nadpisać obiekt pod własnym prefiksem (bucket nie ma wersjonowania). Delete jest odcięty, nadpisanie nie.

### Zarządzany MinIO

Jeżeli host z `backup.s3.endpoint` jest pierwszym hostem grupy `infra`, `cluster-backup-configure`:

1. używa `MINIO_ROOT_USER` i `MINIO_ROOT_PASSWORD` tylko w tymczasowym pliku `0600` na hoście infra;
2. tworzy bucket i marker właściciela przy użyciu root credentials;
3. tworzy konto usługowe `galera-backup-<cluster>` (zapis, bez delete) oraz konto retencji `galera-backup-prune-<cluster>` (ewentualnie skrócone do 32 znaków, patrz wyżej) — obydwa z polityką ograniczoną do jednego bucketu i prefiksu klastra;
4. zapisuje parę zapisu na wszystkich węzłach Galery i hoście restore, a parę retencji wyłącznie na `backup.scheduler.host`;
5. usuwa tymczasowe dane root.

Ponowne configure zachowuje działającą parę kluczy i zbiega politykę — nie rotuje klucza przy każdym uruchomieniu.

Rotacja zarządzanego MinIO:

1. w oknie serwisowym usuń konto usługowe `galera-backup-<cluster>` (i/lub konto retencji — pełna lub skrócona nazwa `galera-backup-prune-…`) w konsoli administracyjnej MinIO;
2. załaduj root credentials poza repozytorium;
3. uruchom `make cluster-backup-configure CLUSTER=<name>`;
4. uruchom ręczny backup i potwierdzany restore.

Usunięty klucz przestaje działać natychmiast. Configure tworzy nową parę i rozprowadza ją na oba hosty. Dla zewnętrznego S3 albo SMB ustaw nowe scoped credentials w środowisku i wykonaj te same kroki 3–4.

## Operacje

```bash
# Zainstaluj/zbiegnij runner, sekrety i cron.
make cluster-backup-configure CLUSTER=<name>

# Wykonaj backup teraz, niezależnie od scheduler.mode.
make cluster-backup CLUSTER=<name>

# Odtwórz najnowszy kompletny backup na izolowanym hoście grupy restore.
make cluster-restore-drill CLUSTER=<name> CONFIRM=yes
```

Restore jest odrzucany bez `CONFIRM=yes`, na schedulerze lub na hoście należącym do grup `galera`/`proxysql`. Odtwarzany MariaDB działa standalone bez wsrep; runner wykonuje checksum, `mariadb-check --all-databases`, bezpieczną ekstrakcję tar i co najmniej jedno zapytanie do każdej tabeli użytkownika. Puste tabele są prawidłowe; brak baz lub tabel użytkownika nie jest.

Wymóg "co najmniej jedna baza użytkownika" jest celowy — backup pustego serwera nie dowodzi odtwarzalności danych. Na klastrze z ruchem dane po prostu są. Świeżo postawiony klaster laboratoryjny jest jednak pusty i drill kończy się `E_INTEGRITY: Restored database contains zero user databases`, dopóki nie zapisano czegokolwiek. Do tego służy `make lab-seed-smoke CLUSTER=<name>` (`playbooks/lab_seed_smoke.yml`): zakłada `isa_test.restore_probe` z jednym wierszem, jest idempotentny (drugi przebieg raportuje `changed=0`) i odmawia pracy poza `cluster.profile: laboratory`. Nie jest wpięty w `f10_backup` ani `f10_restore` — automatyka wdrożeniowa nie zakłada schematów na klastrze z danymi.

## Artefakty i retencja

Kompletny artefakt `galera-<cluster>-YYYYmmdd-HHMMSS` zawiera:

```text
backup.tar.enc
backup.sha256
metadata.json
```

Backend filesystem publikuje przez katalog `.partial-*` i atomowy rename. S3 publikuje metadata jako ostatni obiekt, po zwrotnym odczycie i checksumie payloadu; porażka uploadu usuwa i ponownie sprawdza cały prefiks próby. Nieusuwalna pozostałość jest raportowana jako błąd cleanup, nie jako sukces. Retencja usuwa wyłącznie kompletne artefakty własnego klastra.

Każdy backend ma marker `galera-backup-owner.json`. Obcy `cluster_name`, uszkodzony albo brakujący marker blokuje operację (`E_OWNER_CONFLICT`); runner nie przejmuje cudzego katalogu lub bucketu. Dla zarządzanego MinIO marker tworzy configure z root credentials, a scoped konto może go wyłącznie czytać.

`immutable_or_offsite_copy` opisuje własność infrastruktury, nie włącza jej automatycznie. Repozytoryjny MinIO na hoście `infra` jest poza węzłami Galery, ale nie jest przez to niezmienny ani off-site. Wymaga niezależnej ochrony storage, replikacji lub drugiej kopii, jeżeli ma przetrwać utratę całej lokalizacji.

## Ścieżki operacyjne

| Element | Ścieżka |
|---|---|
| Runner | `/opt/galera-backup/galera-backup` |
| Konfiguracja | `/opt/galera-backup/clusters/<cluster>/config.json` |
| Sekrety | `/opt/galera-backup/clusters/<cluster>/secrets.env` |
| Stan | `/opt/galera-backup/clusters/<cluster>/state.json` |
| Zdarzenia JSONL | `/opt/galera-backup/clusters/<cluster>/events.jsonl` |
| Cron | `/etc/cron.d/galera-backup-<cluster>` |
| Lock | `/run/lock/galera-backup-<cluster>.lock` |
| Staging | `/var/tmp/galera-backup/<cluster>/` |
| Metryki node_exporter | `/var/lib/node_exporter/textfile_collector/galera_backup-<cluster>.prom` |

## Diagnostyka

```bash
sudo systemctl status crond
sudo journalctl -t galera-backup-<cluster> --since today
sudo jq . /opt/galera-backup/clusters/<cluster>/state.json
sudo tail -n 20 /opt/galera-backup/clusters/<cluster>/events.jsonl
sudo cat /var/lib/node_exporter/textfile_collector/galera_backup-<cluster>.prom
sudo findmnt --mountpoint <mount-point>
```

Najczęstsze kody:

| Kod | Znaczenie / działanie |
|---|---|
| `E_LOCKED` | Inna operacja tego klastra trzyma lock; nie uruchamiaj drugiego backupu. |
| `E_GALERA` | Klaster lub `mariadb-backup` nie spełnia warunków; sprawdź wsrep i log. |
| `E_STORAGE_AUTH` | S3 odrzucił credentials; napraw scoped key i ponownie configure. |
| `E_STORAGE` | Backend/mount/publikacja/cleanup nie powiodły się; sprawdź mount, kernel i pojemność. |
| `E_OWNER_CONFLICT` | Bucket lub katalog należy do innego klastra; nie usuwaj markera w celu obejścia. |
| `E_INTEGRITY` | Checksum, metadata, tar lub restore nie spełnia kontraktu; artefaktu nie używaj. |
| `E_RESTORE_CONFIRM` | Restore nie ma potwierdzenia albo działa na niedozwolonym hoście. |

Po usunięciu przyczyny wykonaj ręczny backup, `lab-backup-verify` dla S3 oraz potwierdzany restore. Dwa alerty F15 obserwują osobno ostatnią porażkę (`galera_backup_last_run_success`) i wiek ostatniego sukcesu (`galera_backup_last_success_unixtime`); brak metryk krytycznych alarmuje.
