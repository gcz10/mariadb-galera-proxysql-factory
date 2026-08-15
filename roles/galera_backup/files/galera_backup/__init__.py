"""Pakiet `galera_backup` — moduly wydzielane z monolitu `galera-backup`.

Dekompozycja idzie krokami opisanymi w docs/plans/galera-backup-decomposition.md.
Zasada nadrzedna: entrypoint `galera-backup` pozostaje FASADA — re-eksportuje
wszystko, co wydzielone, wiec `tests/unit/galera_backup_testlib.py` laduje go
`SourceFileLoader`-em i widzi te same symbole co przed rozbiciem. Dzieki temu
136 testow przechodzi NIETKNIETYCH na kazdym posrednim commicie.

Czego NIE wolno tu przenosic bez zmiany testow: symbole podmieniane przez
`patch.object(self.mod, ...)` musza pozostac rozwiazywalne z przestrzeni nazw
entrypointa RAZEM ze swoimi wywolujacymi. Inaczej mock przestaje dzialac po
cichu — test dalej przechodzi, ale niczego nie sprawdza. Na dzis sa to:
assert_scheduler_is_not_writer, get_storage_backend, perform_physical_backup,
query_galera_vars, restore_default_context, selinux_is_enabled.
"""
