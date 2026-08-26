"""Pakiet `galera_backup` — moduly wydzielane z monolitu `galera-backup`.

Dekompozycja monolitu zakonczona: cala logika orkiestracji mieszka w tym
pakiecie (`pipeline.py`, `runner.py`, `storage/`, `config.py`, `locking.py`,
`state.py`, `secrets.py`, `errors.py`, `fsutil.py`, `textutil.py`), a
plik wykonywalny `galera-backup` to 21-liniowy wrapper wolajacy `pipeline.main()`.

Testy jednostkowe (`tests/unit/galera_backup_testlib.py`) laduja modul
`galera_backup.pipeline` bezposrednio przez `importlib`, bo to tam mieszkaja
symbole podmieniane przez `patch.object(self.mod, ...)` razem ze swoimi
wywolujacymi.
"""
