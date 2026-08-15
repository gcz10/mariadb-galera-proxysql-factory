"""Typ bledu runnera i skladanie bledow lancuchowych."""


class BackupError(Exception):
    def __init__(self, code: str, public_message: str):
        super().__init__(f"[{code}] {public_message}")
        self.code = code
        self.public_message = public_message


def combine_failures(primary: Exception, cleanup: Exception, default_code: str) -> "BackupError":
    """Zloz blad glowny z bledem sprzatania, zachowujac kod tego pierwszego.

    Sprzatanie, ktore padlo po realnej awarii, nie moze przeslonic jej przyczyny —
    kod wyjscia pochodzi od bledu glownego, komunikat niesie oba.
    """
    if isinstance(primary, BackupError):
        error_code = primary.code
        primary_message = primary.public_message
    else:
        error_code = default_code
        primary_message = str(primary)
    cleanup_message = cleanup.public_message if isinstance(cleanup, BackupError) else str(cleanup)
    return BackupError(
        error_code,
        f"{primary_message}; cleanup also failed: {cleanup_message}",
    )
