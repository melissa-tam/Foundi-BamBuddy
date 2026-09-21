"""
Code quality tests for BamBuddy backend.

These tests check for common anti-patterns and code quality issues
that could cause runtime errors but aren't caught by normal tests.
"""

import ast
from pathlib import Path

import pytest

# Get the backend source directory
BACKEND_DIR = Path(__file__).parent.parent.parent / "app"


# Safe imports that are commonly re-imported in functions without issues
# These are typically imported at the START of a function, not midway through
SAFE_REIMPORT_NAMES = {
    "logging",
    "re",
    "os",
    "sys",
    "json",
    "Path",
    "datetime",
    "timedelta",
    "asyncio",
    "time",
    "typing",
    "Optional",
    "List",
    "Dict",
    "Any",
    "Union",
}


class DangerousImportVisitor(ast.NodeVisitor):
    """AST visitor that detects dangerous import patterns.

    Specifically looks for cases where:
    1. A name is imported at module level
    2. The same name is imported locally in a function
    3. The name is USED before the local import in that function

    This pattern causes 'cannot access local variable' errors.
    """

    def __init__(self):
        self.module_imports: set[str] = set()
        self.dangerous_imports: list[tuple[str, int, str, int]] = []  # (name, import_line, function, first_use_line)
        self.current_function: str | None = None
        self.function_start_line: int = 0
        self.in_function = False

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            name = alias.asname or alias.name
            if not self.in_function:
                self.module_imports.add(name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        for alias in node.names:
            name = alias.asname or alias.name
            if not self.in_function:
                self.module_imports.add(name)
        self.generic_visit(node)

    def _check_function(self, node):
        """Check a function for dangerous import patterns."""
        if not self.in_function:
            return

        # Skip safe reimports
        # Collect all local imports in this function
        local_imports: dict[str, int] = {}  # name -> line number
        name_uses: dict[str, int] = {}  # name -> first use line number

        for child in ast.walk(node):
            # Find local imports
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    name = alias.asname or alias.name
                    if name in self.module_imports and name not in SAFE_REIMPORT_NAMES:
                        local_imports[name] = child.lineno

            # Find name uses
            if isinstance(child, ast.Name):
                if child.id not in name_uses:
                    name_uses[child.id] = child.lineno

        # Check for dangerous pattern: use before import
        for name, import_line in local_imports.items():
            if name in name_uses:
                first_use = name_uses[name]
                if first_use < import_line:
                    self.dangerous_imports.append((name, import_line, self.current_function, first_use))

    def visit_FunctionDef(self, node: ast.FunctionDef):
        old_function = self.current_function
        old_in_function = self.in_function
        old_start_line = self.function_start_line

        self.current_function = node.name
        self.in_function = True
        self.function_start_line = node.lineno

        self._check_function(node)
        self.generic_visit(node)

        self.current_function = old_function
        self.in_function = old_in_function
        self.function_start_line = old_start_line

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        old_function = self.current_function
        old_in_function = self.in_function
        old_start_line = self.function_start_line

        self.current_function = node.name
        self.in_function = True
        self.function_start_line = node.lineno

        self._check_function(node)
        self.generic_visit(node)

        self.current_function = old_function
        self.in_function = old_in_function
        self.function_start_line = old_start_line


def find_import_shadowing(file_path: Path) -> list[tuple[str, int, str]]:
    """Find cases where local imports shadow module-level imports AND are used before import.

    Returns list of (name, line_number, function_name) tuples.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        visitor = DangerousImportVisitor()
        visitor.visit(tree)
        # Convert (name, import_line, function, first_use_line) to (name, import_line, function)
        return [(name, import_line, func) for name, import_line, func, _ in visitor.dangerous_imports]
    except SyntaxError:
        return []  # Skip files with syntax errors


def get_python_files(directory: Path) -> list[Path]:
    """Get all Python files in a directory recursively."""
    return list(directory.rglob("*.py"))


class TestImportShadowing:
    """Tests for import shadowing anti-pattern."""

    def test_no_import_shadowing_in_main(self):
        """Check main.py has no import shadowing issues.

        This test would have caught the ArchiveService scoping bug.
        """
        main_file = BACKEND_DIR / "main.py"
        if not main_file.exists():
            pytest.skip("main.py not found")

        shadows = find_import_shadowing(main_file)

        if shadows:
            error_msg = "Import shadowing detected in main.py:\n"
            for name, line, func in shadows:
                error_msg += f"  - '{name}' at line {line} in function '{func}' shadows module-level import\n"
            error_msg += "\nThis can cause 'cannot access local variable' errors."
            pytest.fail(error_msg)

    def test_no_import_shadowing_in_services(self):
        """Check service files have no import shadowing issues."""
        services_dir = BACKEND_DIR / "services"
        if not services_dir.exists():
            pytest.skip("services directory not found")

        all_shadows = []
        for py_file in get_python_files(services_dir):
            shadows = find_import_shadowing(py_file)
            for name, line, func in shadows:
                all_shadows.append((py_file.name, name, line, func))

        if all_shadows:
            error_msg = "Import shadowing detected in services:\n"
            for filename, name, line, func in all_shadows:
                error_msg += f"  - {filename}: '{name}' at line {line} in function '{func}'\n"
            pytest.fail(error_msg)

    def test_no_import_shadowing_in_routes(self):
        """Check route files have no import shadowing issues."""
        routes_dir = BACKEND_DIR / "api" / "routes"
        if not routes_dir.exists():
            pytest.skip("routes directory not found")

        all_shadows = []
        for py_file in get_python_files(routes_dir):
            shadows = find_import_shadowing(py_file)
            for name, line, func in shadows:
                all_shadows.append((py_file.name, name, line, func))

        if all_shadows:
            error_msg = "Import shadowing detected in routes:\n"
            for filename, name, line, func in all_shadows:
                error_msg += f"  - {filename}: '{name}' at line {line} in function '{func}'\n"
            pytest.fail(error_msg)


# --- The equipment-fault resolution family (2026-09-17) -----------------------------

# WHO may read the resolution-class vocabulary. The four class literals and the
# ``RESOLVES_ON`` table they key are the rule's own words: the model DEFINES them, the
# store reads them into one pure function, the rule table turns them into verdicts, and
# the incidents read surface reports the class as data. Anybody else spelling one is a
# SIXTH closer being born — which is the shape this wave existed to end (011-H2S
# 2026-09-17: six closers each carrying their own ``if resolution == …`` chain, so a new
# evidence had to be added in six places and was therefore added in none).
_RESOLUTION_VOCABULARY_OWNERS = {
    ("models", "printer_incident.py"),
    ("services", "printer_incidents.py"),
    ("services", "incident_resolution.py"),
    ("api", "routes", "incidents.py"),
}

_RESOLUTION_LITERALS = {
    "RESOLUTION_WIRE",
    "RESOLUTION_REPAIR",
    "RESOLUTION_OPERATOR",
    "RESOLUTION_DECLARED",
    "RESOLVES_ON",
}

# WHO may end an equipment-fault row. The family's own closers plus the TWO lanes the
# rule table's module docstring DECLARES as out-of-table, each because it owns state the
# table cannot see:
#   * ``farm_policy``  — the plate-vision first-trip re-check (the windowed trip count,
#                        the vouching test, the gate it raises when it cannot vouch);
#   * ``service_hold`` — ``exit``, the declared hold's own and only counterpart verb.
# A close appearing anywhere else fails this test BY CONSTRUCTION. That is the point:
# the allowlist is a declaration, so a new closer has to be argued for in a diff rather
# than added in silence.
_INCIDENT_CLOSERS = {
    ("services", "printer_incidents.py"),  # defines it
    ("services", "spool_recovery.py"),
    ("services", "pause_recovery.py"),
    ("services", "farm_policy.py"),
    ("services", "service_hold.py"),
}


# --- The operator stop, and who may send one (2026-09-19) --------------------------

# WHO may call ``print_control.stop_as_operator``. The pair it sends (MQTT ``print.stop``
# plus the user-stopped mark) MEANS "a human pressed Stop", and the whole terminal
# disposition downstream is built on that meaning: the unit lands ``cancelled`` with a
# ``stop_source``, the run holds, RESUME tops the deficit back up. Two routes are the two
# Stop buttons an operator can actually press:
#   * ``api/routes/printers.py``    — the printer card's stop;
#   * ``api/routes/print_queue.py`` — the queue page's stop.
# The third caller used to be ``service_hold.quiesce``, and deleting it IS the 2026-09-19
# ruling: **no mode verb ends a print.** Entering maintenance mode or deactivating a
# printer stands the FARM's own actions down — a sweep it commanded, a dispatch not yet on
# the wire — while the print, which belongs to the operator, runs to its own terminal. A
# future mode verb that reaches for this pair is a silent plate cancellation, and this
# test is what turns it into a CI failure with a decision to argue in the diff.
_OPERATOR_STOP_CALLERS = {
    ("services", "print_control.py"),  # defines it
    ("api", "routes", "printers.py"),
    ("api", "routes", "print_queue.py"),
}


def _relative_parts(py_file: Path) -> tuple[str, ...]:
    return py_file.relative_to(BACKEND_DIR).parts


def _scan_operator_stops(py_file: Path) -> list[tuple[str, int]]:
    """Every CALL of ``stop_as_operator``, however the module was imported.

    AST rather than grep for the same reason the incident scan is: the verb is named in
    prose all over this codebase (it is the thing the rulings are about), and pinning the
    prose would make the rule unwritable.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "stop_as_operator":
            hits.append(("stop_as_operator()", node.lineno))
    return hits


class TestOperatorStopOwnership:
    """Only an operator's Stop button may send an operator stop.

    A SOURCE pin, deliberately: the failure it catches is a new mode verb that quietly
    cancels the running print — behaviour every other test in the suite is happy with,
    because the resulting cancel is perfectly well-formed. It is the ATTRIBUTION that is
    a lie.
    """

    def test_only_the_two_stop_routes_send_an_operator_stop(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts in _OPERATOR_STOP_CALLERS:
                continue
            for symbol, line in _scan_operator_stops(py_file):
                strays.append(f"  - {'/'.join(parts)}:{line} calls {symbol}")

        if strays:
            pytest.fail(
                "Something outside the two Stop routes sends an operator stop:\n"
                + "\n".join(strays)
                + "\n\nNo mode verb ends a print (2026-09-19): maintenance mode and deactivation "
                "stand the FARM's actions down and leave the operator's print running. If a new "
                "caller really is a human pressing Stop, add it here with its reason."
            )

    def test_the_allowlisted_stop_routes_are_still_there(self):
        """The liveness half: an allowlist whose entries have all moved away stops
        policing anything, and the scan above would then pass on an empty set."""
        # ``print_control`` DEFINES the verb with a ``def`` rather than calling it, so it
        # is allowlisted but never a hit — every other entry must be.
        callers = _OPERATOR_STOP_CALLERS - {("services", "print_control.py")}
        senders = {_relative_parts(f) for f in get_python_files(BACKEND_DIR) if _scan_operator_stops(f)}
        assert senders == callers


def _scan_resolution_vocabulary(py_file: Path) -> list[tuple[str, int]]:
    """Every USE (never a mention in prose) of a class literal or ``resolution_class``.

    AST, not grep: the four literals are named in docstrings and comments all over this
    codebase — that is documentation, and pinning it would make the rule unwritable.
    Only ``Name`` / ``Attribute`` / ``Call`` nodes count.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _RESOLUTION_LITERALS:
            hits.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in _RESOLUTION_LITERALS:
            hits.append((node.attr, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "resolution_class":
                hits.append(("resolution_class()", node.lineno))
    return hits


def _scan_incident_closes(py_file: Path) -> list[tuple[str, int]]:
    """Every call that ENDS an incident row: ``printer_incidents.close`` and the bulk
    ``close_open_for_printer``, however the module was imported."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            owner = func.value
            qualified = isinstance(owner, ast.Name) and owner.id == "printer_incidents"
            if func.attr == "close_open_for_printer" or (qualified and func.attr == "close"):
                hits.append((f"printer_incidents.{func.attr}()", node.lineno))
        elif isinstance(func, ast.Name) and func.id == "close_open_for_printer":
            hits.append(("close_open_for_printer()", node.lineno))
    return hits


class TestIncidentResolutionOwnership:
    """The resolution rule has ONE owner, and rows are ended in declared places only.

    Both scans are SOURCE pins rather than behaviour pins, deliberately: the failure
    they catch is a correct-looking new branch in the wrong module, which every
    behaviour test in the suite would happily pass.
    """

    def test_the_class_vocabulary_stays_inside_the_family(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts in _RESOLUTION_VOCABULARY_OWNERS:
                continue
            for symbol, line in _scan_resolution_vocabulary(py_file):
                strays.append(f"  - {'/'.join(parts)}:{line} uses {symbol}")

        if strays:
            pytest.fail(
                "The resolution-class vocabulary escaped its owners:\n"
                + "\n".join(strays)
                + "\n\nAsk incident_resolution.resolve(row, occasion, ctx) instead — the class "
                "literals belong to the rule table, and a closer that reads them is a second "
                "copy of the rule."
            )

    def test_only_declared_lanes_close_an_incident(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts in _INCIDENT_CLOSERS:
                continue
            for symbol, line in _scan_incident_closes(py_file):
                strays.append(f"  - {'/'.join(parts)}:{line} calls {symbol}")

        if strays:
            pytest.fail(
                "A new equipment-fault closer appeared outside the declared lanes:\n"
                + "\n".join(strays)
                + "\n\nThe family's closers live in spool_recovery / pause_recovery and select "
                "their lane through incident_resolution's table; farm_policy (plate-vision "
                "first-trip re-check) and service_hold.exit are the two DECLARED exceptions, "
                "named in incident_resolution's module docstring. A third needs the same "
                "declaration."
            )

    def test_the_allowlisted_lanes_are_still_there(self):
        """The liveness half. An allowlist whose entries have all moved away silently
        stops policing anything, and both scans would then pass on an empty set."""
        found_vocabulary = {
            parts
            for parts in (_relative_parts(f) for f in get_python_files(BACKEND_DIR))
            if parts in _RESOLUTION_VOCABULARY_OWNERS
        }
        assert found_vocabulary == _RESOLUTION_VOCABULARY_OWNERS

        # ``printer_incidents`` DEFINES the verb rather than calling it through the
        # module name, so it is allowlisted but never a hit — every other entry must be.
        callers = _INCIDENT_CLOSERS - {("services", "printer_incidents.py")}
        closers_with_a_close = {_relative_parts(f) for f in get_python_files(BACKEND_DIR) if _scan_incident_closes(f)}
        assert closers_with_a_close == callers


# --- The derived-3MF cache mechanism, and which module owns it (2026-09-19) --------

# The content-addressed cache MECHANISM — the sha256 key, the system-temp caller copies,
# the atomic ``os.replace`` install, the ``*.3mf`` glob the byte-bounded LRU walks, the
# per-key lock dict — lives ONCE, in ``services/derived_3mf_cache.py``, because a second
# lane (the per-dispatch print file) builds derived artifacts the same way.
# ``eject/build_cache.py`` is a FACADE over it and decides only what is genuinely the
# eject lane's own: WHAT is built, into WHICH namespace, under WHICH byte cap, and with
# WHICH error vocabulary its callers catch.
#
# The failure this pins is the mechanism growing back where it used to live — one "just
# this once" hashlib key, one local lock dict, one extra glob in the facade. Every
# behaviour test in the suite passes that, and it is how one data directory ends up with
# two eviction policies and two locking rules that disagree under load.
_CACHE_FACADE = ("services", "eject", "build_cache.py")
_CACHE_CORE = ("services", "derived_3mf_cache.py")

_MECHANISM_MODULES = {"hashlib", "tempfile", "glob"}


def _scan_cache_mechanism(py_file: Path) -> list[tuple[str, int]]:
    """Every USE of the cache mechanism's own primitives: the key hash, the temp-file
    maker, the atomic install, the artifact glob and the per-key lock.

    AST rather than grep, like the scans above: the facade's docstring NAMES all of
    these (explaining what it deliberately no longer does), and pinning the prose would
    make the rule unwritable.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _MECHANISM_MODULES:
                    hits.append((f"import {alias.name}", node.lineno))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _MECHANISM_MODULES:
                hits.append((f"import {root}", node.lineno))
            elif root == "asyncio" and any(alias.name == "Lock" for alias in node.names):
                hits.append(("asyncio.Lock", node.lineno))
        elif isinstance(node, ast.Attribute):
            owner = node.value
            owner_name = owner.id if isinstance(owner, ast.Name) else None
            if owner_name in _MECHANISM_MODULES:
                hits.append((f"{owner_name}.{node.attr}", node.lineno))
            elif owner_name == "os" and node.attr == "replace":
                hits.append(("os.replace", node.lineno))
            elif owner_name == "asyncio" and node.attr == "Lock":
                hits.append(("asyncio.Lock", node.lineno))
            elif node.attr == "glob":
                hits.append((".glob()", node.lineno))
    return hits


class TestDerivedCacheOwnership:
    """One cache mechanism, in one module, with the eject lane as a facade over it.

    A SOURCE pin: the failure it catches is a perfectly working private helper in the
    wrong module — a second implementation of a cache that is already canonical.
    """

    def test_the_eject_facade_carries_no_cache_mechanism(self):
        facade = BACKEND_DIR.joinpath(*_CACHE_FACADE)
        assert facade.exists(), f"{'/'.join(_CACHE_FACADE)} is gone — this pin now polices nothing"

        strays = [
            f"  - {'/'.join(_CACHE_FACADE)}:{line} uses {symbol}" for symbol, line in _scan_cache_mechanism(facade)
        ]
        if strays:
            pytest.fail(
                "The derived-3MF cache mechanism reappeared in the eject facade:\n"
                + "\n".join(strays)
                + "\n\nCall derived_3mf_cache.get_or_build(..., namespace=, max_bytes=) instead — the "
                "facade owns WHAT is built and where, never HOW it is keyed, installed, locked or evicted."
            )

    def test_the_mechanism_is_still_in_the_core(self):
        """The liveness half: if the mechanism moved somewhere else entirely, the scan
        above would pass on an empty facade while policing nothing at all."""
        core = BACKEND_DIR.joinpath(*_CACHE_CORE)
        assert core.exists(), f"{'/'.join(_CACHE_CORE)} is gone — the cache core moved without this pin"

        found = {symbol for symbol, _ in _scan_cache_mechanism(core)}
        for primitive in ("import hashlib", "import tempfile", "os.replace", ".glob()", "asyncio.Lock"):
            assert primitive in found, f"{'/'.join(_CACHE_CORE)} no longer uses {primitive}"


# --- Which bytes a dispatch uploads, and who decides that (2026-09-19) --------------

# The question "what does the printer actually receive" has ONE owner:
# ``services/dispatch_file.py``. It builds the transform stack (the chute-prime rewrite,
# then the upstream per-model snippets), DERIVES the cache key from that stack, and hands
# ``print_scheduler._start_print`` a verdict. Two things are pinned here:
#
#   * the two transform entry points — ``chute_prime.rewrite_head`` and
#     ``threemf_tools.apply_gcode_snippets`` — are called from the seam and nowhere else;
#   * the two settings keys that SELECT them are spelled only by the seam that reads them
#     and by the two modules that define the setting (the schema twin, the PUT coercion
#     whitelist).
#
# The failure this catches is a second rewrite site: a perfectly working injection added
# back into the scheduler, a route or a recovery lane. Every behaviour test would pass —
# and the cache key would stop describing the bytes, which is precisely how a kill switch
# turned off still serves chute-primed files out of the cache.
_DISPATCH_SEAM = ("services", "dispatch_file.py")

_DISPATCH_TRANSFORM_CALLS = {"rewrite_head", "apply_gcode_snippets"}

_DISPATCH_SETTING_KEYS = {"farm_chute_prime_enabled", "gcode_snippets"}

_DISPATCH_SETTING_SPELLERS = {
    _DISPATCH_SEAM,
    ("schemas", "settings.py"),  # defines the typed field + its update twin
    ("api", "routes", "settings.py"),  # the boolean-coercion whitelist
}

# What ``print_scheduler`` may no longer import from ``threemf_tools``: it does not
# rewrite print bytes any more, it ASKS. ``inject_gcode_into_3mf`` is in the set although
# it is deleted — re-adding it is exactly the regression, and a set that names it fails
# on the import rather than on a behaviour nobody wrote a test for.
_INJECTION_IMPORT_SYMBOLS = {"inject_gcode_into_3mf", "apply_gcode_snippets", "transform_plate_gcode"}


def _scan_dispatch_decisions(py_file: Path) -> list[tuple[str, int]]:
    """Every CALL of a transform entry point, and every EXACT spelling of a settings key.

    AST, and exact string equality for the keys: both keys are named in prose in this
    codebase (the seam's own docstring explains the kill switch), and a substring scan
    would pin the documentation instead of the code.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _DISPATCH_TRANSFORM_CALLS:
                hits.append((f"{name}()", node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _DISPATCH_SETTING_KEYS:
            hits.append((f'"{node.value}"', node.lineno))
    return hits


class TestDispatchFileOwnership:
    """One seam decides which bytes a dispatch uploads, and one module reads its switch."""

    def test_only_the_seam_applies_a_dispatch_transform(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts == _DISPATCH_SEAM:
                continue
            for symbol, line in _scan_dispatch_decisions(py_file):
                if symbol.startswith('"') and parts in _DISPATCH_SETTING_SPELLERS:
                    continue
                strays.append(f"  - {'/'.join(parts)}:{line} uses {symbol}")

        if strays:
            pytest.fail(
                "Something outside the dispatch-file seam decides which bytes a dispatch uploads:\n"
                + "\n".join(strays)
                + "\n\nCall services/dispatch_file.build_dispatch_file(...) instead, and add a new "
                "per-dispatch transform as one more STEP in its stack (a fingerprint plus a pure "
                "bytes -> bytes). A second rewrite site means a cache key that no longer describes "
                "the bytes it names."
            )

    def test_the_scheduler_imports_no_injection_symbol(self):
        """The one print-upload lane asks the seam; it does not reach for the injector."""
        scheduler = BACKEND_DIR / "services" / "print_scheduler.py"
        assert scheduler.exists(), "print_scheduler.py is gone — this pin now polices nothing"

        tree = ast.parse(scheduler.read_text(encoding="utf-8"))
        strays = [
            f"  - print_scheduler.py:{node.lineno} imports {alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("threemf_tools")
            for alias in node.names
            if alias.name in _INJECTION_IMPORT_SYMBOLS
        ]
        if strays:
            pytest.fail(
                "print_scheduler reached back into threemf_tools for an injection symbol:\n" + "\n".join(strays)
            )

    def test_the_seam_still_owns_both_transforms_and_both_keys(self):
        """The liveness half: an ownership pin whose owner has moved away polices nothing,
        and the scan above would then pass on a codebase with no seam at all."""
        seam = BACKEND_DIR.joinpath(*_DISPATCH_SEAM)
        assert seam.exists(), f"{'/'.join(_DISPATCH_SEAM)} is gone — the seam moved without this pin"

        found = {symbol for symbol, _ in _scan_dispatch_decisions(seam)}
        for expected in ("rewrite_head()", "apply_gcode_snippets()", '"farm_chute_prime_enabled"', '"gcode_snippets"'):
            assert expected in found, f"{'/'.join(_DISPATCH_SEAM)} no longer uses {expected}"


# --- Fleet history: who may write it (2026-09-21) ---------------------------------

# The observation log and the episode ledger are APPEND-shaped records of what was
# observed and what was measured, and every reader downstream — the availability
# classifier, the timeline, every series on the Fleet tab — trusts that the rows came
# from the one sampler and the two instruments. A second writer would not fail any
# behaviour test: it would quietly produce rows that look identical and mean something
# else (a hand-closed span, a fabricated episode), and nothing later could tell them
# apart. So the writers are pinned by SOURCE, like the incident closers above.
_SPAN_WRITER = ("services", "fleet_activity.py")
_EPISODE_WRITER = ("services", "cycle_episodes.py")

# WHO may hand a measurement to the ledger. The writer itself (it defines the verb)
# plus the two owners that actually TIME an episode: the eject terminal, which reads
# the sweep's own start echo, and the cooldown prep's retirement, which is the end of
# the cooling episode. A third caller is a duration nobody measured.
_EPISODE_CALLERS = {
    _EPISODE_WRITER,
    ("services", "farm_policy.py"),
    ("services", "eject", "cooldown_prep.py"),
}

_FLEET_MODELS = {"PrinterObservationSpan": _SPAN_WRITER, "FarmCycleEpisode": _EPISODE_WRITER}

# The Core statements that WRITE a table, as opposed to selecting from it. Reads are
# deliberately unrestricted — the whole point of recording raw rows is that anything
# may classify them.
_TABLE_WRITE_VERBS = {"insert", "update", "delete"}


def _scan_fleet_history_writes(py_file: Path) -> list[tuple[str, str, int]]:
    """Every ORM construction of a fleet-history row, and every Core write of its table.

    Returns ``(model_name, symbol, lineno)``. AST rather than grep because both model
    names appear in prose all over these modules' docstrings, which is where the rule
    is explained.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _FLEET_MODELS:
            hits.append((name, f"{name}()", node.lineno))
        elif name in _TABLE_WRITE_VERBS and node.args:
            target = node.args[0]
            model = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
            if model in _FLEET_MODELS:
                hits.append((model, f"{name}({model})", node.lineno))
    return hits


def _scan_episode_notes(py_file: Path) -> list[tuple[str, int]]:
    """Every CALL of ``note_episode``, however the module was imported."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "note_episode":
            hits.append(("note_episode()", node.lineno))
    return hits


class TestFleetActivityOwnership:
    """One sampler writes the observation log; one leaf writes the episode ledger."""

    def test_each_fleet_history_table_has_exactly_one_writer(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            for model, symbol, line in _scan_fleet_history_writes(py_file):
                if parts == _FLEET_MODELS[model]:
                    continue
                strays.append(f"  - {'/'.join(parts)}:{line} uses {symbol}")

        if strays:
            pytest.fail(
                "Something outside the declared writer writes fleet history:\n"
                + "\n".join(strays)
                + "\n\nObservation spans are written ONLY by services/fleet_activity.py (the sampler "
                "owns the run-length encoding, and a row written anywhere else breaks the one-open-span "
                "invariant the partial unique index exists to hold); cycle episodes are written ONLY by "
                "services/cycle_episodes.note_episode, which the two measuring owners call. Reading "
                "either table is unrestricted — recording raw and classifying at read time is the point."
            )

    def test_only_the_measuring_owners_note_an_episode(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts in _EPISODE_CALLERS:
                continue
            for symbol, line in _scan_episode_notes(py_file):
                strays.append(f"  - {'/'.join(parts)}:{line} calls {symbol}")

        if strays:
            pytest.fail(
                "A new cycle-episode writer appeared outside the two instruments:\n"
                + "\n".join(strays)
                + "\n\nAn episode row is a DURATION somebody measured — the eject terminal reads the "
                "sweep's own start echo, and cooldown_prep.end() is the end of the cooling episode. A "
                "caller that did not time the episode is recording a guess."
            )

    def test_the_declared_writers_are_still_there(self):
        """The liveness half. An allowlist whose entries have all moved away silently
        stops policing anything, and both scans would then pass on an empty set."""
        for model, owner in _FLEET_MODELS.items():
            path = BACKEND_DIR.joinpath(*owner)
            assert path.exists(), f"{'/'.join(owner)} is gone — the writer moved without this pin"
            found = {symbol for _, symbol, _ in _scan_fleet_history_writes(path)}
            assert f"{model}()" in found, f"{'/'.join(owner)} no longer constructs {model}"

        # ``cycle_episodes`` DEFINES the verb rather than calling it, so it is
        # allowlisted but never a hit — both hook modules must be.
        hooks = _EPISODE_CALLERS - {_EPISODE_WRITER}
        callers = {_relative_parts(f) for f in get_python_files(BACKEND_DIR) if _scan_episode_notes(f)}
        assert callers == hooks


class TestModuleImports:
    """Tests for module import health."""

    def test_all_modules_importable(self):
        """Verify all Python modules can be imported without errors.

        This catches syntax errors and missing dependencies.

        IMPORTANT: We must NOT ``del sys.modules[name]`` to force a fresh
        import here. ``backend.app.main`` is a stateful module — re-importing
        it builds NEW module-level dicts (_timelapse_baselines,
        _expected_prints, _active_prints, …) and re-runs ``root_logger.
        addHandler(console_handler)``. Any test that already bound those
        names via ``from backend.app.main import _timelapse_baselines`` now
        holds a stale reference, while production code resolves the symbol
        through the new module instance — they're two different dicts. CI
        under -n 2 puts test_code_quality.py on the same worker as
        test_print_start_assigns_printer_id_to_vp_archive.py and
        test_timelapse_baseline_restart_recovery.py, and those tests see
        their mock_archive un-mutated / their baseline dict empty even
        though production logged the mutations went through. Local -n 30
        spreads the tests across workers and the collision never happens.

        ``importlib.import_module`` already covers the "is this importable"
        check — it returns the cached module if cached, or runs the import
        machinery if not. Either way, an import-time error surfaces here.
        """
        import importlib

        # Modules to test importing
        modules = [
            "backend.app.main",
            "backend.app.services.bambu_mqtt",
            "backend.app.services.printer_manager",
            "backend.app.services.archive",
            "backend.app.services.notification_service",
            "backend.app.services.smart_plug_manager",
        ]

        errors = []
        for module_name in modules:
            try:
                importlib.import_module(module_name)
            except Exception as e:
                errors.append(f"{module_name}: {type(e).__name__}: {e}")

        if errors:
            pytest.fail("Failed to import modules:\n" + "\n".join(errors))


class TestLogErrorPatterns:
    """Tests that use log capture to detect runtime errors."""

    def test_mqtt_message_processing_no_errors(self, capture_logs):
        """Test that MQTT message processing doesn't log errors."""
        from backend.app.services.bambu_mqtt import BambuMQTTClient

        client = BambuMQTTClient(
            ip_address="192.168.1.100",
            serial_number="TEST123",
            access_code="12345678",
        )
        client.on_print_start = lambda data: None
        client.on_print_complete = lambda data: None

        # Process a realistic print lifecycle
        messages = [
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/test.gcode", "subtask_name": "Test"}},
            {"print": {"gcode_state": "RUNNING", "gcode_file": "/test.gcode", "mc_percent": 50}},
            {"print": {"gcode_state": "FINISH", "gcode_file": "/test.gcode", "subtask_name": "Test"}},
        ]

        for msg in messages:
            client._process_message(msg)

        assert not capture_logs.has_errors(), f"Errors during MQTT processing:\n{capture_logs.format_errors()}"
