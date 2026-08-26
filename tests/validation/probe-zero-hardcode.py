#!/usr/bin/env python3
"""Verify cluster portability — no hardcoded cluster data (ISC-59), and a new
cluster requires only a new clusters/<name>/ directory (ISC-58).

ISC-59: roles, playbooks and templates MUST NOT contain hardcoded cluster-specific
        data. Node names as a `hosts:`/`delegate_to:` target, and lab IPs / cluster
        names in any code line, break a second cluster.
ISC-58: a new cluster requires ONLY clusters/<name>/. The example-cluster template
        must exist; roles/playbooks must not reference a specific cluster directory.

Comments (#) and task `name:` fields are excluded for node-name checks (they're
documentation); lab IPs and cluster names are flagged in any non-comment line.

NAZWY INSTANCJI CZYTAMY Z REPO, NIE Z LISTY WZORCOW. Do 2026-08-26 ta sonda
deklarowala w kontrakcie "cluster names in any code line", a wykrywala wylacznie
stare KSZTALTY nazw (`gnode\\d+`, `galera\\d+`) i adresy `172.28/29.x`. Dlatego
`@test "$(CLUSTER)" = "newclaude17-r9"` w Makefile przechodzil przez bramke i
zyl w niej dopoki klaster nie zostal skasowany — cel stal sie nieuruchamialny.
Nazwy bierzemy teraz z katalogow `clusters/*/` i `platform/*/`, wiec kazda nowa
instancja jest objeta w chwili powstania, bez dopisywania wzorca.

GDZIE NAZWA JEST DEFEKTEM, A GDZIE DOWODEM:
  * w kodzie — tylko w linii, ktora STERUJE wykonaniem. Komentarz
    "zmierzone 2026-08-25 na `orion-r9`" to proweniencja dowodu i zostaje;
    usuwanie takich sladow kosztowaloby wiecej niz daje.
  * w README — wszedzie. README jest kontraktem produktu, wiec nazwa zywej
    maszyny gnije w nim przy pierwszej wymianie floty. Stan floty daje
    `make fleet-state`, historia mieszka w `docs/records/`.

DOMYSLNY CEL MUSI BYC SZABLONEM: `PLATFORM ?= shared` kierowal kazde
`make platform-*` bez argumentu na konkretna, w dodatku juz nieistniejaca
warstwe. Slowo `shared` jest za generyczne na wzorzec tekstowy (`isa-shared-*`,
`/etc/mysql/app/shared`), wiec sprawdzamy je regula o wartosciach domyslnych.
"""

import os
import re
import sys

SCAN_DIRS = ["playbooks", "roles"]
# Makefile jest interfejsem operatora i tez potrafi przypiac fabryke do jednego
# klastra — `GALERA_VMS ?= gnode1 ...` kierowalo cel destrukcyjny na nazwy VM
# nieistniejace w innych klastrach. Skanujemy go tymi samymi wzorcami.
SCAN_FILES = ["Makefile"]
SKIP_DIRS = {"clusters", "tests", "docs", "versions", ".git", "node_modules"}

# README to kontrakt produktu, nie zapis stanu — nazwa instancji jest w nim
# defektem niezaleznie od tego, czy stoi w zdaniu, czy w przykladzie komendy.
DOC_FILES = ["README.md"]
# Katalogi, ktore nie sa instancjami: szablony i schematy.
TEMPLATES = {"example-cluster", "example", "schema"}
# Nazwa za generyczna na dopasowanie tekstowe — pilnuje jej regula domyslnych celow.
GENERIC_NAMES = {"shared"}
# Zmienna sterujaca -> katalog szablonu, ktory jest jej jedyna poprawna wartoscia.
TEMPLATE_DEFAULTS = {"CLUSTER": "clusters/example-cluster", "PLATFORM": "platform/example"}

HARDCODE_PATTERNS = [
    (re.compile(r"172\.(28|29)\.0\.\d+"), "lab IP"),
    (re.compile(
        r"\b(gnode\d+|g9t?node\d+|pnode\d+|rnode\d+|r9t?node\d+"
        r"|galera\d+|infranode)\b"
    ), "lab node name"),
]


def instance_names():
    """Nazwy zywych i archiwalnych instancji, odczytane z repo w chwili uruchomienia."""
    names = set()
    for root in ("clusters", "platform"):
        if not os.path.isdir(root):
            continue
        for entry in os.listdir(root):
            if not os.path.isdir(os.path.join(root, entry)):
                continue
            if entry in TEMPLATES or entry in GENERIC_NAMES:
                continue
            names.add(entry)
    return names


def instance_pattern(names):
    """Alternatywa od najdluzszej nazwy, zeby `kobalt-r9` nie raportowal sie jako `kobalt`."""
    if not names:
        return None
    ordered = sorted(names, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in ordered) + r")\b")


def scan_file(path, instances=None):
    findings = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.lstrip()
                is_comment = stripped.startswith("#")
                # Dokumentacja: komentarz, `name:` zadania, tekst pomocy `##`
                # oraz komunikat w `echo` — to opis, nie miejsce uzycia.
                is_name = re.match(r'name:\s*["\']', stripped) is not None
                is_help = "##" in line or stripped.startswith("@#")
                # Miejsce, w ktorym nazwa wezla realnie kieruje operacja:
                # w YAML-u cel polaczenia, w Makefile przypisanie zmiennej.
                is_connection = bool(re.search(r'\b(hosts|delegate_to)\s*:', line)) or \
                    bool(re.match(r'[A-Za-z_][A-Za-z0-9_]*\s*[:?+]?=', stripped))
                for pat, label in HARDCODE_PATTERNS:
                    for m in pat.finditer(line):
                        if label == "lab node name":
                            # node name only matters as a connection target
                            if is_comment or is_name or is_help or not is_connection:
                                continue
                        elif is_comment or is_help:
                            continue
                        findings.append((lineno, label, m.group(0), stripped[:80]))
                if instances is not None:
                    # Nazwa instancji jest defektem tylko tam, gdzie STERUJE
                    # wykonaniem. Komentarz i tekst pomocy niosa proweniencje
                    # dowodu ("zmierzone na X") i zostaja nietkniete.
                    if not (is_comment or is_help or is_name):
                        for m in instances.finditer(line):
                            findings.append(
                                (lineno, "cluster name", m.group(0), stripped[:80])
                            )
    except OSError:
        pass
    return findings


def iter_files(extensions, include_standalone=False):
    for top in SCAN_DIRS:
        if not os.path.isdir(top):
            continue
        for root, dirs, files in os.walk(top):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if fname.endswith(extensions):
                    yield os.path.join(root, fname)
    # Pliki bez rozszerzenia (Makefile) nie pasuja do filtra po suffiksie,
    # a ISC-59 dotyczy ich tak samo — kod kierujacy operacja na konkretny klaster.
    if include_standalone:
        for path in SCAN_FILES:
            if os.path.isfile(path):
                yield path


def scan_doc(path, instances):
    """README: nazwa instancji jest defektem w kazdej linii, takze w prozie."""
    findings = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                for m in instances.finditer(line):
                    findings.append((lineno, m.group(0), line.strip()[:80]))
    except OSError:
        pass
    return findings


def default_target_findings():
    """Domyslna wartosc zmiennej sterujacej musi wskazywac szablon, nie instancje."""
    findings = []
    try:
        with open("Makefile", encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                match = re.match(r"(CLUSTER|PLATFORM)\s*\?=\s*(\S+)", line)
                if not match:
                    continue
                variable, value = match.group(1), match.group(2)
                template = TEMPLATE_DEFAULTS[variable]
                if os.path.basename(template) != value:
                    findings.append(
                        f"Makefile:{lineno} {variable} ?= {value} — domyslny cel musi "
                        f"wskazywac szablon ({os.path.basename(template)}), nie instancje"
                    )
    except OSError:
        pass
    return findings


def main():
    failures = []
    instances = instance_pattern(instance_names())

    # ISC-59: no hardcoded cluster data in roles/playbooks/templates/Makefile
    hits = []
    for path in iter_files((".yml", ".yaml", ".j2", ".py", ".sh", ".cnf"),
                           include_standalone=True):
        for lineno, label, match, snippet in scan_file(path, instances):
            hits.append(f"{path}:{lineno} {label} {match!r}: {snippet}")
    if hits:
        failures.append("ISC-59 — hardcoded cluster data found in roles/playbooks/Makefile:")
        failures.extend(f"  - {h}" for h in hits[:20])

    # ISC-59: README jest kontraktem produktu — bez nazw instancji w ogole.
    doc_hits = []
    if instances is not None:
        for path in DOC_FILES:
            for lineno, match, snippet in scan_doc(path, instances):
                doc_hits.append(f"{path}:{lineno} {match!r}: {snippet}")
    if doc_hits:
        failures.append(
            "ISC-59 — nazwa instancji w README (stan floty daje `make fleet-state`, "
            "historia mieszka w docs/records/):"
        )
        failures.extend(f"  - {h}" for h in doc_hits[:20])

    failures.extend(default_target_findings())

    # ISC-58: example-cluster template exists
    for required in ("clusters/example-cluster/cluster.yml",
                     "clusters/example-cluster/inventory.yml",
                     "platform/example/platform.yml"):
        if not os.path.exists(required):
            failures.append(f"ISC-58 — portable template missing: {required}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS: ISC-58/59 — factory portable: 0 hardcoded cluster data in "
          "roles/playbooks/Makefile/README; templates present; default targets "
          "point at templates; new cluster = only clusters/<name>/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
