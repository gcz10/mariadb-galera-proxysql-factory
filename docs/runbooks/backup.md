# Runbook: Backup

**Status:** STUB — do uzupełnienia w F10
**Powiązane ISC:** ISC-32, ISC-33, ISC-34, ISC-35, ISC-39

## Przeznaczenie

Wykonywanie backupu klastra Galera przez `mariadb-backup`.

## Procedura

```bash
# 1. Wybierz węzeł niebędący aktywnym writerem
# 2. Kontroluj wsrep_desync i powrót do Synced
make cluster-backup CLUSTER=<name>
```

## Wymagania (ISC)

- ISC-32: Backup opuszcza klaster (SMB mount / S3)
- ISC-33: Backup zaszyfrowany (gpg/age)
- ISC-34: Checksum poprawny (`sha256sum`)
- ISC-35: Metadata: wersja MariaDB, czas, cluster name, wsrep seqno
- ISC-39: Backup nie degraduje writera (flow control threshold)

## Weryfikacja

```bash
# Sprawdź że backup opuścił klaster
findmnt -t cifs | grep <backup_mount>

# Sprawdź szyfrowanie
file <backup_file>  # powinno wskazywać encrypted data

# Sprawdź checksum
sha256sum -c <backup_file>.sha256

# Sprawdź metadata
cat <backup_file>.meta.json | jq .
```

## Harmonogram

- Full backup: codziennie 02:00 (`0 2 * * *`)
- Restore drill: cotygodniowo niedziela 04:00 (`0 4 * * 0`)
- Retencja: SMB 14d, S3 30d (ADR-003)
