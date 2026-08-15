"""Ladowanie modulu runnera na potrzeby testow jednostkowych.

HISTORIA I POWOD OBECNEGO KSZTALTU
----------------------------------
Wczesniej ten loader czytal plik wykonywalny `roles/galera_backup/files/galera-backup`
przez `SourceFileLoader`, bo cala logika byla w jednym pliku. Po dekompozycji
`galera-backup` jest juz tylko 21-liniowym wrapperem wolajacym `main()`.

Ladujemy `galera_backup.pipeline`, a NIE wrapper, i to jest wymog poprawnosci,
nie wygody. Testy podmieniaja szesc symboli przez `patch.object(self.mod, ...)`:

    query_galera_vars, get_storage_backend, perform_physical_backup,
    assert_scheduler_is_not_writer, restore_default_context, selinux_is_enabled

`patch.object` podmienia wiazanie w KONKRETNEJ przestrzeni nazw. Gdyby `self.mod`
wskazywal na wrapper, a `run_backup` mieszkal w `pipeline`, to wywolania wewnatrz
`run_backup` rozwiazywalyby sie w `pipeline` i mock nie przechwycilby niczego —
suita zostalaby zielona, nie sprawdzajac nic. Dlatego rdzen i chronione symbole
sa w jednym module, a loader celuje wlasnie w niego.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = WORKSPACE_ROOT / "roles" / "galera_backup" / "files"
EXECUTABLE_PATH = PACKAGE_ROOT / "galera-backup"


def load_galera_backup_module():
    if str(PACKAGE_ROOT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_ROOT))
    if importlib.util.find_spec("galera_backup") is None:
        raise ImportError(f"Cannot import package 'galera_backup' from {PACKAGE_ROOT}")
    module = importlib.import_module("galera_backup.pipeline")
    return importlib.reload(module)
