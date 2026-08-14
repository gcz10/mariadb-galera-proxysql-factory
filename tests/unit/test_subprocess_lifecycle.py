"""Unit tests verifying subprocess lifecycle, process group reaping, and signal handling.

Verifies that:
1. A subprocess timeout terminates and reaps the whole child process group.
2. A real SIGTERM delivered to the runner process terminates its child, so no
   orphaned process keeps writing after the parent exits.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from galera_backup_testlib import load_galera_backup_module  # noqa: E402

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TESTS_UNIT_DIR = WORKSPACE_ROOT / "tests" / "unit"

DRIVER_TEMPLATE = """\
import sys, signal
sys.path.insert(0, %(testlib_dir)r)
from galera_backup_testlib import load_galera_backup_module

mod = load_galera_backup_module()


def _handler(signum, frame):
    raise mod.BackupError("E_STORAGE", "interrupted by signal %%d" %% signum)


signal.signal(signal.SIGTERM, _handler)

runner = mod.CommandRunner(secret_values=[])
child_script = (
    "import os, time; "
    "open(%(pid_file)r, 'w').write(str(os.getpid())); "
    "time.sleep(60)"
)
try:
    runner.run([sys.executable, "-c", child_script], timeout=60)
except BaseException:
    pass
"""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SubprocessTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_galera_backup_module()

    def test_timeout_terminates_and_reaps_child(self):
        runner = self.mod.CommandRunner(secret_values=[])
        start = time.monotonic()
        with self.assertRaises(self.mod.BackupError) as ctx:
            runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
        elapsed = time.monotonic() - start

        self.assertEqual(ctx.exception.code, "E_SUBPROCESS")
        self.assertIn("timed out", ctx.exception.public_message)
        self.assertLess(elapsed, 5.0, "Timeout path must kill the child promptly")


class SignalPropagationTests(unittest.TestCase):
    """Runs a real child process under CommandRunner and delivers SIGTERM to the parent."""

    def test_sigterm_to_parent_does_not_orphan_child(self):
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "child.pid"
            driver = Path(td) / "driver.py"
            driver.write_text(
                DRIVER_TEMPLATE % {"testlib_dir": str(TESTS_UNIT_DIR), "pid_file": str(pid_file)},
                encoding="utf-8",
            )

            parent = subprocess.Popen([sys.executable, str(driver)], cwd=str(WORKSPACE_ROOT))

            deadline = time.monotonic() + 20
            child_pid = None
            while time.monotonic() < deadline:
                if pid_file.exists():
                    text = pid_file.read_text().strip()
                    if text.isdigit():
                        child_pid = int(text)
                        break
                time.sleep(0.1)

            self.assertIsNotNone(child_pid, "Child process must publish its PID")
            self.assertTrue(_pid_alive(child_pid), "Child must be running before SIGTERM")

            parent.send_signal(signal.SIGTERM)
            parent.wait(timeout=20)

            gone_deadline = time.monotonic() + 10
            while time.monotonic() < gone_deadline and _pid_alive(child_pid):
                time.sleep(0.2)

            self.assertFalse(
                _pid_alive(child_pid),
                f"Child pid {child_pid} must be terminated, not orphaned after parent SIGTERM",
            )


if __name__ == "__main__":
    unittest.main()
