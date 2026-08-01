import sys
import importlib.machinery
import importlib.util
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
EXECUTABLE_PATH = WORKSPACE_ROOT / "roles" / "galera_backup" / "files" / "galera-backup"


def load_galera_backup_module():
    loader = importlib.machinery.SourceFileLoader("galera_backup_module", str(EXECUTABLE_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec from {EXECUTABLE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module
