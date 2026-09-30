#!/usr/bin/env python3
"""Read-only witness of the newest SMB backup artifact, executed ON a backup host.

`probe-backup.py` ships this file over the existing ansible ad-hoc channel to the
backup scheduler host and judges the evidence it prints. The reader only
collects facts: it never mounts, never copies an artifact to disk, never writes
to the share or to the host, and never prints a credential.

Where the facts come from (installed by roles/galera_backup, not invented here):
  * /opt/galera-backup/clusters/<cluster>/config.json  - backend source and
    mount_point the runner really uses;
  * /opt/galera-backup/clusters/<cluster>/secrets.env  - SMB credentials, parsed
    by the installed `galera_backup.config.load_secrets`;
  * <mount_point>/<cluster>/galera-<cluster>-YYYYMMDD-HHMMSS/{backup.tar.enc,
    backup.sha256,metadata.json} and <mount_point>/<cluster>/galera-backup-owner.json
    - the layout written by the installed FilesystemBackend / SMBBackend.

Two read routes, tried in this order:
  1. "mount": the share is ALREADY mounted at the installed mount_point with the
     configured source and every option the runner itself requires. The runner
     mounts only for the duration of a run, so this normally holds only inside
     a run window.
  2. "smbclient": the files are streamed over SMB 3.1.1 with encryption by the
     `smbclient` binary if it is installed; the password travels in the child
     environment only, never in argv, and the payload goes straight into a
     SHA-256 (nothing is written to disk).
When neither route exists the answer is status "unavailable" - never a pass.

Standard library only; it must run on the stock python3 of the host (3.9+).
It is executed from a base64 argument, so it must not rely on __file__.
"""
from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

RESULT_PREFIX = "BACKUP_EVIDENCE="
OWNER_MARKER = "galera-backup-owner.json"
ARTIFACT_FILES = ("backup.tar.enc", "backup.sha256", "metadata.json")
MOUNTINFO_PATH = "/proc/self/mountinfo"
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
# The same set SMBBackend._validate_observed_mount demands of a live mount.
REQUIRED_MOUNT_OPTIONS = frozenset({"vers=3.1.1", "seal", "nosuid", "nodev", "noexec"})
CLUSTER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SOURCE_RE = re.compile(r"^//([^/]+)/([^/]+)$")
MAX_TEXT_BYTES = 1 << 20
MAX_LISTING_BYTES = 4 << 20
CHUNK_BYTES = 1 << 20
MAGIC_BYTES = 8
SMBCLIENT_REQUEST_TIMEOUT = 60
NT_STATUS_RE = re.compile(rb"NT_STATUS_[A-Z0-9_]+")
# The only statuses that mean "measured: that path does not exist".
ABSENT_STATUSES = frozenset(
    {
        "NT_STATUS_NO_SUCH_FILE",
        "NT_STATUS_OBJECT_NAME_NOT_FOUND",
        "NT_STATUS_OBJECT_PATH_NOT_FOUND",
    }
)
LS_LINE_RE = re.compile(
    r"^  (?P<name>\S(?:.*?\S)?)\s+(?P<attr>[A-Za-z]+)\s+(?P<size>[0-9]+)\s\s+\S.*$"
)
LS_FOOTER_RE = re.compile(r"[0-9]+ blocks of size [0-9]+\. [0-9]+ blocks available")
OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


class Unavailable(Exception):
    """The state could not be read; the caller must report UNDETERMINED."""


def normalize_smb_source(source):
    # Same rule as galera_backup.textutil.normalize_smb_source.
    return str(source).replace("\\", "/").rstrip("/").casefold()


def _errname(exc):
    return errno.errorcode.get(getattr(exc, "errno", None) or 0, type(exc).__name__)


def _check_deadline(deadline, what):
    if time.monotonic() > deadline:
        raise Unavailable("timed out while %s" % what)


def backup_name_re(cluster):
    return re.compile("^galera-%s-[0-9]{8}-[0-9]{6}$" % re.escape(cluster))


class PayloadDigest:
    """SHA-256, size and leading bytes of a stream that is never stored."""

    def __init__(self):
        self._sha = hashlib.sha256()
        self.size = 0
        self.magic = b""

    def feed(self, chunk):
        if len(self.magic) < MAGIC_BYTES:
            self.magic += chunk[: MAGIC_BYTES - len(self.magic)]
        self._sha.update(chunk)
        self.size += len(chunk)

    def result(self):
        return {
            "magic_hex": self.magic.hex(),
            "sha256": self._sha.hexdigest(),
            "size": self.size,
        }


# === installed configuration ================================================


def read_installed_config(install_root, cluster):
    path = os.path.join(install_root, "clusters", cluster, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise Unavailable(
            "installed backup config %s is unreadable (%s)" % (path, _errname(exc))
        )
    if not isinstance(data, dict):
        raise Unavailable("installed backup config %s is not a JSON object" % path)
    backend = data.get("backend")
    if not isinstance(backend, dict):
        backend = {}
    return {
        "format_version": data.get("format_version"),
        "cluster_name": data.get("cluster_name"),
        "backend_type": backend.get("type"),
        "source": backend.get("source"),
        "mount_point": backend.get("mount_point"),
    }


def config_mismatches(installed, cluster, declared_source, declared_mount_point):
    """Drift between the installed runner config and the declared cluster.yml."""
    problems = []
    if installed["format_version"] != 1:
        problems.append(
            "installed config format_version %r is not 1" % (installed["format_version"],)
        )
    if installed["cluster_name"] != cluster:
        problems.append(
            "installed config cluster_name %r != %r" % (installed["cluster_name"], cluster)
        )
    if installed["backend_type"] != "smb":
        problems.append(
            "installed config backend.type %r is not 'smb'" % (installed["backend_type"],)
        )
    installed_source = str(installed["source"] or "")
    if normalize_smb_source(installed_source) != normalize_smb_source(declared_source):
        problems.append(
            "installed backend.source %r != declared %r" % (installed_source, declared_source)
        )
    if installed["mount_point"] != declared_mount_point:
        problems.append(
            "installed backend.mount_point %r != declared %r"
            % (installed["mount_point"], declared_mount_point)
        )
    return problems


# === route 1: an already-mounted share ======================================


def _unescape_mountinfo(field):
    return OCTAL_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), field)


def parse_mountinfo(text):
    entries = []
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            continue
        head = left.split()
        tail = right.split()
        if len(head) < 6 or len(tail) < 3:
            continue
        entries.append(
            {
                "mount_point": _unescape_mountinfo(head[4]),
                "mount_options": head[5],
                "fstype": tail[0],
                "source": _unescape_mountinfo(tail[1]),
                "super_options": tail[2],
            }
        )
    return entries


def classify_mount(entries, mount_point, source):
    """(usable, reason). Only the topmost mount at the path counts."""
    real = os.path.realpath(mount_point)
    matching = [entry for entry in entries if entry["mount_point"] == real]
    if not matching:
        return False, "the share is not mounted at %s" % mount_point
    top = matching[-1]
    if top["fstype"] != "cifs":
        return False, "%s is mounted as %s, not cifs" % (mount_point, top["fstype"])
    if normalize_smb_source(top["source"]) != normalize_smb_source(source):
        return False, "%s is mounted from a different source than %s" % (mount_point, source)
    observed = set()
    for field in (top["mount_options"], top["super_options"]):
        for option in field.split(","):
            option = option.strip().lower()
            if option:
                observed.add(option)
    missing = sorted(REQUIRED_MOUNT_OPTIONS - observed)
    if missing:
        return False, "%s lacks required mount options: %s" % (mount_point, ", ".join(missing))
    return True, ""


def mount_usable(mountinfo_path, mount_point, source):
    try:
        with open(mountinfo_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return False, "the mount table is unreadable (%s)" % _errname(exc)
    return classify_mount(parse_mountinfo(text), mount_point, source)


def _open_regular(path):
    """fd of a regular file, None when absent or not a regular file."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR, errno.ENOTDIR):
            return None
        raise Unavailable("cannot open %s (%s)" % (os.path.basename(path), _errname(exc)))
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError as exc:
        os.close(fd)
        raise Unavailable("cannot stat %s (%s)" % (os.path.basename(path), _errname(exc)))
    if not regular:
        os.close(fd)
        return None
    return fd


def _read_fd_limited(fd, limit):
    """Read at most limit bytes; (data, complete)."""
    chunks = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[:limit], False


class MountReader:
    route = "mount"

    def __init__(self, mount_point, cluster, deadline):
        self.cluster_dir = os.path.join(mount_point, cluster)
        self.name_re = backup_name_re(cluster)
        self.deadline = deadline

    def read_text(self, relative, absent_ok=True):
        path = os.path.join(self.cluster_dir, relative)
        _check_deadline(self.deadline, "reading %s" % relative)
        fd = _open_regular(path)
        if fd is None:
            return None, False
        try:
            data, complete = _read_fd_limited(fd, MAX_TEXT_BYTES)
        except OSError as exc:
            raise Unavailable("cannot read %s (%s)" % (relative, _errname(exc)))
        finally:
            os.close(fd)
        if not complete:
            return None, True
        return data.decode("utf-8", "replace"), False

    def list_backups(self):
        try:
            entries = os.listdir(self.cluster_dir)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise Unavailable("cannot list %s (%s)" % (self.cluster_dir, _errname(exc)))
        names = []
        for name in entries:
            if not self.name_re.match(name):
                continue
            try:
                mode = os.lstat(os.path.join(self.cluster_dir, name)).st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise Unavailable("cannot stat %s (%s)" % (name, _errname(exc)))
            if stat.S_ISDIR(mode):
                names.append(name)
        return sorted(names)

    def present_files(self, name):
        files = {}
        for filename in ARTIFACT_FILES:
            path = os.path.join(self.cluster_dir, name, filename)
            try:
                files[filename] = stat.S_ISREG(os.lstat(path).st_mode)
            except FileNotFoundError:
                files[filename] = False
            except OSError as exc:
                raise Unavailable("cannot stat %s/%s (%s)" % (name, filename, _errname(exc)))
        return files

    def hash_payload(self, name):
        path = os.path.join(self.cluster_dir, name, "backup.tar.enc")
        fd = _open_regular(path)
        if fd is None:
            raise Unavailable("backup.tar.enc of %s disappeared while reading" % name)
        digest = PayloadDigest()
        try:
            while True:
                _check_deadline(self.deadline, "reading the payload of %s" % name)
                chunk = os.read(fd, CHUNK_BYTES)
                if not chunk:
                    break
                digest.feed(chunk)
        except OSError as exc:
            raise Unavailable("cannot read the payload of %s (%s)" % (name, _errname(exc)))
        finally:
            os.close(fd)
        return digest.result()


# === route 2: smbclient, no mount ===========================================


class _Drain(threading.Thread):
    """Keeps the child's stderr pipe empty; retains only a bounded prefix."""

    def __init__(self, stream):
        threading.Thread.__init__(self)
        self.daemon = True
        self.stream = stream
        self.data = b""

    def run(self):
        try:
            while True:
                chunk = self.stream.read(4096)
                if not chunk:
                    break
                if len(self.data) < 16384:
                    self.data += chunk[: 16384 - len(self.data)]
        except (OSError, ValueError):
            pass


def _terminate(proc):
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _read_stream_limited(stream, limit):
    chunks = []
    total = 0
    while total <= limit:
        chunk = stream.read(min(CHUNK_BYTES, limit + 1 - total))
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)[:limit], False


def parse_smbclient_listing(text):
    """[(name, attr, size)] from `ls`; None when the listing is not complete."""
    if not LS_FOOTER_RE.search(text):
        return None
    entries = []
    for line in text.splitlines():
        match = LS_LINE_RE.match(line)
        if match and match.group("name") not in (".", ".."):
            entries.append((match.group("name"), match.group("attr"), int(match.group("size"))))
    return entries


class SmbclientReader:
    route = "smbclient"

    def __init__(self, exe, source, cluster, username, password, domain, deadline):
        self.exe = exe
        self.source = source
        self.cluster = cluster
        self.username = username
        self.domain = domain
        self.deadline = deadline
        self.name_re = backup_name_re(cluster)
        self._password = password
        self._sizes = {}

    def _argv(self, command):
        argv = [
            self.exe,
            self.source,
            "-U",
            self.username,
            "-t",
            str(SMBCLIENT_REQUEST_TIMEOUT),
            "--client-protection=encrypt",
            "--option=client min protocol=SMB3_11",
        ]
        # -E also redirects ls output to stderr; use it only to keep get payloads clean.
        if command.startswith("get "):
            argv += ["-E"]
        if self.domain:
            argv += ["-W", self.domain]
        return argv + ["-c", command]

    def _execute(self, command, consume):
        """Run one command; returns (returncode, consumed value, NT_STATUS names)."""
        _check_deadline(self.deadline, "starting smbclient")
        # The password is only ever in the child's environment, never in argv.
        env = {"PATH": SAFE_PATH, "LC_ALL": "C", "PASSWD": self._password}
        try:
            proc = subprocess.Popen(
                self._argv(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise Unavailable("cannot start smbclient (%s)" % _errname(exc))
        drain = _Drain(proc.stderr)
        drain.start()
        try:
            value, complete = consume(proc.stdout)
            if complete:
                remaining = max(1.0, self.deadline - time.monotonic())
                try:
                    returncode = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise Unavailable("smbclient did not exit in time")
            else:
                _terminate(proc)
                returncode = None
        except BaseException:
            _terminate(proc)
            raise
        finally:
            drain.join(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        statuses = sorted({m.decode("ascii") for m in NT_STATUS_RE.findall(drain.data)})
        return returncode, value, statuses

    def _fail(self, what, returncode, statuses):
        detail = ",".join(statuses) if statuses else "rc=%s" % returncode
        raise Unavailable("smbclient %s failed (%s)" % (what, detail))

    def _listing(self, mask):
        """Entries of `ls`; [] only when the path measurably does not exist."""
        rc, data, statuses = self._execute(
            "ls %s" % mask,
            lambda stream: _read_stream_limited(stream, MAX_LISTING_BYTES),
        )
        if rc is None:
            raise Unavailable("smbclient listing of %s is too large" % mask)
        text = data.decode("utf-8", "replace")
        statuses = set(statuses) | {m.decode("ascii") for m in NT_STATUS_RE.findall(data)}
        if rc != 0:
            if statuses and statuses <= ABSENT_STATUSES:
                return []
            self._fail("listing", rc, sorted(statuses))
        entries = parse_smbclient_listing(text)
        if entries is None:
            raise Unavailable("smbclient listing of %s was not complete" % mask)
        return entries

    def list_backups(self):
        entries = self._listing("%s/galera-%s-*" % (self.cluster, self.cluster))
        return sorted(
            name for name, attr, _ in entries if "D" in attr and self.name_re.match(name)
        )

    def present_files(self, name):
        entries = self._listing("%s/%s/*" % (self.cluster, name))
        present = {}
        for entry_name, attr, size in entries:
            if "D" not in attr:
                present[entry_name] = size
        files = {}
        for filename in ARTIFACT_FILES:
            files[filename] = filename in present
            if filename in present:
                self._sizes["%s/%s" % (name, filename)] = present[filename]
        return files

    def read_text(self, relative, absent_ok=True):
        size = self._sizes.get(relative)
        if size is not None and size > MAX_TEXT_BYTES:
            return None, True
        rc, data, statuses = self._execute(
            "get %s/%s -" % (self.cluster, relative),
            lambda stream: _read_stream_limited(stream, MAX_TEXT_BYTES),
        )
        if rc is None:
            return None, True
        if rc != 0:
            if absent_ok and statuses and set(statuses) <= ABSENT_STATUSES:
                return None, False
            self._fail("read of %s" % relative, rc, statuses)
        return data.decode("utf-8", "replace"), False

    def hash_payload(self, name):
        digest = PayloadDigest()

        def consume(stream):
            while True:
                _check_deadline(self.deadline, "streaming the payload of %s" % name)
                chunk = stream.read(CHUNK_BYTES)
                if not chunk:
                    return None, True
                digest.feed(chunk)

        rc, _, statuses = self._execute(
            "get %s/%s/backup.tar.enc -" % (self.cluster, name), consume
        )
        if rc != 0:
            self._fail("payload read of %s" % name, rc, statuses)
        return digest.result()


def load_smb_credentials(install_root, cluster):
    """(username, password, domain) via the INSTALLED runner's own loader."""
    if install_root not in sys.path:
        sys.path.insert(0, install_root)
    try:
        from galera_backup.config import load_secrets
        from galera_backup.errors import BackupError
    except Exception as exc:
        raise Unavailable(
            "the installed galera_backup package is not importable (%s)" % type(exc).__name__
        )
    env_path = Path(install_root) / "clusters" / cluster / "secrets.env"
    try:
        secrets = load_secrets(env_path, "smb")
    except BackupError as exc:
        raise Unavailable("the installed SMB credentials are unusable (%s)" % exc.code)
    username = secrets.get("GALERA_BACKUP_SMB_USERNAME", "")
    password = secrets.get("GALERA_BACKUP_SMB_PASSWORD", "")
    domain = secrets.get("GALERA_BACKUP_SMB_DOMAIN", "")
    if "%" in username:
        # smbclient would split USER at '%' into user and password.
        raise Unavailable("the installed SMB username contains '%', unsupported by smbclient")
    return username, password, domain


# === evidence ===============================================================


def build_evidence(reader, notes):
    owner_text, owner_oversize = reader.read_text(OWNER_MARKER)
    latest = None
    for name in reversed(reader.list_backups()):
        files = reader.present_files(name)
        # metadata.json is written last by every backend: it is the marker of
        # a finished publication. A newer directory without it is in flight.
        if files["metadata.json"]:
            latest = (name, files)
            break
    evidence = {
        "status": "ok",
        "access": reader.route,
        "notes": notes,
        "owner": {"text": owner_text, "oversize": owner_oversize},
        "backup": None,
    }
    if latest is None:
        return evidence
    name, files = latest
    record = {
        "name": name,
        "files": files,
        "metadata": None,
        "checksum": None,
        "oversize": [],
        "payload": None,
    }
    for filename, key in (("metadata.json", "metadata"), ("backup.sha256", "checksum")):
        if files[filename]:
            text, oversize = reader.read_text("%s/%s" % (name, filename), absent_ok=False)
            record[key] = text
            if oversize:
                record["oversize"].append(filename)
    # Hashing a multi-GB payload is pointless when the set is already broken.
    if all(files.values()):
        record["payload"] = reader.hash_payload(name)
    evidence["backup"] = record
    return evidence


def collect(params, mountinfo_path=MOUNTINFO_PATH):
    cluster = params.get("cluster")
    install_root = params.get("install_root")
    declared_source = params.get("declared_source")
    declared_mount_point = params.get("declared_mount_point")
    timeout = params.get("timeout_seconds")
    if not isinstance(cluster, str) or not CLUSTER_RE.match(cluster):
        raise Unavailable("invalid cluster name in reader parameters")
    if not isinstance(install_root, str) or not install_root.startswith("/"):
        raise Unavailable("invalid install_root in reader parameters")
    if not isinstance(declared_source, str) or not SOURCE_RE.match(declared_source):
        raise Unavailable("declared SMB source must be exactly //server/share")
    if not isinstance(declared_mount_point, str) or not declared_mount_point.startswith("/"):
        raise Unavailable("declared SMB mount_point must be absolute")
    if type(timeout) not in (int, float) or timeout <= 0:
        raise Unavailable("invalid timeout in reader parameters")
    deadline = time.monotonic() + timeout

    installed = read_installed_config(install_root, cluster)
    problems = config_mismatches(installed, cluster, declared_source, declared_mount_point)
    if problems:
        return {"status": "config_mismatch", "problems": problems}

    notes = []
    reader = None
    usable, reason = mount_usable(mountinfo_path, declared_mount_point, declared_source)
    if usable:
        reader = MountReader(declared_mount_point, cluster, deadline)
    else:
        notes.append(reason)
        exe = shutil.which("smbclient", path=SAFE_PATH)
        if exe is None:
            notes.append("smbclient is not installed")
        else:
            try:
                username, password, domain = load_smb_credentials(install_root, cluster)
            except Unavailable as exc:
                raise Unavailable("; ".join(notes + [str(exc)]))
            reader = SmbclientReader(
                exe, declared_source, cluster, username, password, domain, deadline
            )
    if reader is None:
        notes.append("this probe never mounts, so no artifact was read")
        raise Unavailable("; ".join(notes))
    try:
        return build_evidence(reader, notes)
    except Unavailable as exc:
        raise Unavailable("; ".join(notes + [str(exc)]) if notes else str(exc))


def main(argv):
    try:
        params = json.loads(base64.b64decode(argv[-1]).decode("utf-8"))
        evidence = collect(params)
    except Unavailable as exc:
        evidence = {"status": "unavailable", "reason": str(exc)}
    except Exception as exc:  # never leak a traceback that could carry a value
        evidence = {"status": "unavailable", "reason": "reader failed: %s" % type(exc).__name__}
    sys.stdout.write(RESULT_PREFIX + json.dumps(evidence, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
