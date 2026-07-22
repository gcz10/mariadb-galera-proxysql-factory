# ADR-003: Backup — SMB teraz, migracja S3 z retencją 30d

**Data:** 2026-07-22
**Status:** Accepted
**Decydent:** Principal (Interview 2026-07-22)

## Kontekst

Galera nie zastępuje backupu. Backup musi opuszczać klaster (ISC-32), być szyfrowany (ISC-33), z checksum (ISC-34) i metadata (ISC-35). Restore drill obowiązkowy (ISC-37).

Principal wybrał: zasób SMB zamontowany teraz, opcja S3 później.

## Decyzja

**Faza 1: SMB mount** — backup na zamontowany zasób SMB, szyfrowany po stronie klienta (gpg/age).
**Faza 2: Migracja S3** — retencja 30 dni, immutable/object lock (gdy dostępne).

## Uzasadnienie

- SMB już dostępny — najszybszy start
- Szyfrowanie po stronie klienta gwarantuje confidentiality niezależnie od backendu
- Migracja na S3 dodaje immutability i dłuższą retencję (30d vs 14d na SMB)

## Konsekwencje

- ISC-32: backup musi opuszczać hosty klastra — weryfikacja `findmnt -t cifs` (F0)
- ISC-37: restore drill harmonogram — cotygodniowy (niedziela 04:00)
- Retencja SMB: 14 dni (faza 1); S3: 30 dni (faza 2)
- S3 wymaga osobnych ISC dla fazy S3 (immutable, object lock, lifecycle)
- `secrets.example.yml` zawiera `smb.username`, `smb.password`, `smb.share` (Ansible Vault)

## Fog (rozstrzygnięty)

- ~~retencja backupów~~ ROZSTRZYGNIĘTY — SMB 14d teraz, S3 30d później (Decisions 2026-07-22)
