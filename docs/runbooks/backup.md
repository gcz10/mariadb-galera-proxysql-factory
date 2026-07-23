# Runbook: Backup

**Status:** Aktualny (F10 complete)
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
# Weryfikuj backup w off-cluster S3 (ISC-32/33/34/35)
make lab-backup-verify CLUSTER=<name>

# Backup pod obciążeniem nie degraduje writera (ISC-39, lab-only)
make lab-backup-impact CLUSTER=<name>
```

## Harmonogram

- Full backup: codziennie 02:00 (`0 2 * * *`)
- Restore drill: cotygodniowo niedziela 04:00 (`0 4 * * 0`)
- Retencja: SMB 14d, S3 30d (ADR-003)
