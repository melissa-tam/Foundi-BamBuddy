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
    "RESOLUTION_JOB_PAUSE",
    "RESOLUTION_DECLARED",
    "RESOLVES_ON",
    # Keyed by the class literals: a read of it outside the family is a second copy of
    # "can Recover end this" — the store's ``closed_by_recover`` is the one reader.
    "RECOVER_ENDS",
}

# WHO may end an equipment-fault row. The family's own closers plus the ONE lane the
# rule table's module docstring DECLARES as out-of-table, because it owns state the table
# cannot see:
#   * ``service_hold`` — ``exit``, the declared hold's own and only counterpart verb.
# ``farm_policy`` was the second declared lane (the 2026-09-04 plate-vision first-trip
# re-check) until 2026-09-24: the plate-check hold is now a ``job_pause`` row the table
# closes on its own job's resume or terminal, and the farm policy ends no row at all.
# A close appearing anywhere else fails this test BY CONSTRUCTION. That is the point:
# the allowlist is a declaration, so a new closer has to be argued for in a diff rather
# than added in silence.
_INCIDENT_CLOSERS = {
    ("services", "printer_incidents.py"),  # defines it
    ("services", "spool_recovery.py"),
    ("services", "pause_recovery.py"),
    ("services", "service_hold.py"),
}


# --- The operator stop, and who may send one (2026-09-19) --------------------------

# WHO may call ``print_control.stop_as_operator``. The pair it sends (MQTT ``print.stop``
# plus the user-stopped mark) MEANS "a human pressed Stop", and the whole terminal
# disposition downstream is built on that meaning: the unit lands ``cancelled`` with a
# ``stop_source``, the run holds, RESUME tops the deficit back up. Every caller is a Stop
# an operator can actually press:
#   * ``api/routes/printers.py``    — the printer card's stop, and the printer's own HMS
#                                     dialog "Stop printing" (2026-09-24);
#   * ``api/routes/print_queue.py`` — the queue page's stop;
#   * ``api/routes/webhook.py``     — an API client's ``/stop`` and ``/cancel`` (2026-09-24:
#                                     both used to send a bare stop with no mark — and
#                                     ``/cancel`` called a method that does not exist).
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
    ("api", "routes", "webhook.py"),
}

# WHO may send a RAW ``stop_print`` — the MQTT ``print.stop`` WITHOUT the operator mark.
# A bare stop is the FARM ending a job it owns, and there are exactly two such acts:
#   * ``print_control``   — the operator verb itself (the stop, then the mark);
#   * ``eject/remote``    — the eject lane's kill of its OWN sweep (the runtime watchdog,
#                           the start deadline, the re-drive).
# ``printer_manager`` is the per-printer facade that forwards to the client.
# ``pause_recovery``'s plate-check stop is gone (2026-09-24: the printer's plate check
# PAUSES the job for a human, and the farm sends nothing), and so is every route's bare
# stop — an operator's stop goes through ``print_control`` so it carries its mark.
_RAW_STOP_CALLERS = {
    ("services", "print_control.py"),
    ("services", "eject", "remote.py"),
    ("services", "printer_manager.py"),
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


def _scan_raw_stops(py_file: Path) -> list[tuple[str, int]]:
    """Every CALL of ``stop_print`` (the method or a function of that name)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "stop_print":
            hits.append(("stop_print()", node.lineno))
    return hits


class TestRawStopOwnership:
    """A bare ``print.stop`` is the FARM ending its own job — only two lanes may send one."""

    def test_only_the_allowlisted_lanes_send_a_raw_stop(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts in _RAW_STOP_CALLERS:
                continue
            for symbol, line in _scan_raw_stops(py_file):
                strays.append(f"  - {'/'.join(parts)}:{line} calls {symbol}")
        if strays:
            pytest.fail(
                "A raw stop_print is sent outside the allowlisted lanes:\n"
                + "\n".join(strays)
                + "\n\nAn operator's stop goes through print_control.stop_as_operator (it carries the "
                "user-stopped mark, so the terminal records a cancel). A farm lane that must end its own "
                "job is a decision to argue in the diff, and belongs in the allowlist with its reason."
            )

    def test_the_allowlisted_raw_stop_lanes_are_still_there(self):
        """The liveness half: every allowlisted module still sends one."""
        senders = {_relative_parts(f) for f in get_python_files(BACKEND_DIR) if _scan_raw_stops(f)}
        assert senders == _RAW_STOP_CALLERS


class TestTerminalPolicyHook:
    """``main.on_print_complete`` hands every terminal to the farm policy ONCE.

    The hook used to live inside BOTH notification closures (the archive and the
    no-archive path), sharing their sessions and dying with their failures. It is now one
    spawned task with its own session; a second call is how the policy ends up running
    twice for one terminal, or not at all when a notification throws.
    """

    def test_main_calls_the_farm_policy_exactly_once(self):
        tree = ast.parse((BACKEND_DIR / "main.py").read_text(encoding="utf-8"))
        aliases = {"on_terminal"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "backend.app.services.farm_policy":
                aliases |= {alias.asname or alias.name for alias in node.names if alias.name == "on_terminal"}
        calls = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id in aliases)
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "on_terminal"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "farm_policy"
                )
            )
        ]
        assert len(calls) == 1, f"main.py must call farm_policy.on_terminal exactly once, found calls at {calls}"


class TestOperatorStopOwnership:
    """Only an operator's Stop button may send an operator stop.

    A SOURCE pin, deliberately: the failure it catches is a new mode verb that quietly
    cancels the running print — behaviour every other test in the suite is happy with,
    because the resulting cancel is perfectly well-formed. It is the ATTRIBUTION that is
    a lie.
    """

    def test_only_the_stop_routes_send_an_operator_stop(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts in _OPERATOR_STOP_CALLERS:
                continue
            for symbol, line in _scan_operator_stops(py_file):
                strays.append(f"  - {'/'.join(parts)}:{line} calls {symbol}")

        if strays:
            pytest.fail(
                "Something outside the Stop routes sends an operator stop:\n"
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


# --- Putting a plate back has ONE owner (operator ruling 2026-09-24) ----------------

# ``services/requeue.py`` is THE owner of "put a plate back in the queue": every
# automatic path that returns an un-started claim, mints a new attempt or tops up a
# run goes through one of its verbs, which is what makes "a requeued plate is NEXT in
# line" and "an aborted run prints no more plates" true everywhere at once. Before it,
# three callers each released a claim with their own ideas of position and run state,
# and the retry insert lived in farm_policy beside its decisions.
_REQUEUE_OWNER = ("services", "requeue.py")
_QUEUE_BUILDER = ("services", "queue_builder.py")
_LINEAGE_FIELD = "retry_of_id"


def _called_attr(node: ast.Call) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _is_projection(value: ast.expr) -> bool:
    """``item.retry_of_id`` handed on under its own name — a payload READING the chain
    (the queue list's response, the run detail's units), not a row being given one."""
    return isinstance(value, ast.Attribute) and value.attr == _LINEAGE_FIELD


def _scan_lineage_writes(tree: ast.Module) -> list[int]:
    """Every place a row is GIVEN a ``retry_of_id``: an attribute assignment, a keyword
    argument, or a dict entry (a fields dict for a new row) — a projection excepted."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr == _LINEAGE_FIELD for t in targets):
                hits.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == _LINEAGE_FIELD and not _is_projection(node.value):
            hits.append(node.value.lineno)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == _LINEAGE_FIELD and not _is_projection(value):
                    hits.append(key.lineno)
    return hits


def _is_queue_position(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "position"
        and isinstance(node.value, ast.Name)
        and node.value.id == "PrintQueueItem"
    )


def _scan_position_allocations(tree: ast.Module) -> list[tuple[str, int]]:
    """Every hand-rolled queue position: a ``max(PrintQueueItem.position)`` read, or a
    ``PrintQueueItem(position=...)`` construction."""
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_attr(node)
        if name == "max" and node.args and _is_queue_position(node.args[0]):
            hits.append(("max(PrintQueueItem.position)", node.lineno))
        elif name == "PrintQueueItem" and any(kw.arg == "position" for kw in node.keywords):
            hits.append(("PrintQueueItem(position=...)", node.lineno))
    return hits


class TestRequeueOwnership:
    """ONE owner puts a plate back, ONE owner numbers the queue. SOURCE pins, like their
    neighbours: a second release site or a second max-plus-one is perfectly well-formed
    code that every behaviour test passes — it just lands the plate somewhere else."""

    def test_only_the_requeue_owner_releases_an_unstarted_claim(self):
        strays = [
            f"  - {'/'.join(parts)}:{node.lineno}"
            for parts, tree in _app_trees()
            if parts != _REQUEUE_OWNER
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _called_attr(node) == "release_unstarted_claim"
        ]
        if strays:
            pytest.fail(
                "release_unstarted_claim is called outside services/requeue.py:\n"
                + "\n".join(strays)
                + "\n\nReturn an un-started claim through requeue.return_to_queue: it seats the row "
                "NEXT in line and applies the run gate (a paused run stages it, an ended run "
                "cancels it) — the storage transition alone does neither."
            )

    def test_only_the_requeue_owner_writes_lineage(self):
        strays = [
            f"  - {'/'.join(parts)}:{line}"
            for parts, tree in _app_trees()
            if parts != _REQUEUE_OWNER
            for line in _scan_lineage_writes(tree)
        ]
        if strays:
            pytest.fail(
                "A row is given a retry_of_id outside services/requeue.py:\n"
                + "\n".join(strays)
                + "\n\nA new attempt of a plate is requeue.requeue_attempt's: it is the one writer "
                "of the chain (the idempotency guard and the generation index)."
            )

    def test_no_queue_position_is_allocated_outside_queue_builder(self):
        strays = [
            f"  - {'/'.join(parts)}:{line} {shape}"
            for parts, tree in _app_trees()
            if parts != _QUEUE_BUILDER
            for shape, line in _scan_position_allocations(tree)
        ]
        if strays:
            pytest.fail(
                "A queue position is hand-rolled outside services/queue_builder.py:\n"
                + "\n".join(strays)
                + "\n\nCreate rows through create_queue_rows / create_queue_items (or seat an "
                "existing one with seat_at_head / seat_for_repin): the position SCOPE, the scope "
                "lock and the head/tail rule live there and nowhere else."
            )

    def test_the_owners_still_do_it(self):
        """The liveness half: pins that scan for strays pass on an empty tree too."""
        trees = dict(_app_trees())
        requeue_tree = trees[_REQUEUE_OWNER]
        assert any(
            isinstance(node, ast.Call) and _called_attr(node) == "release_unstarted_claim"
            for node in ast.walk(requeue_tree)
        )
        assert _scan_lineage_writes(requeue_tree)
        assert {shape for shape, _ in _scan_position_allocations(trees[_QUEUE_BUILDER])} == {
            "max(PrintQueueItem.position)",
            "PrintQueueItem(position=...)",
        }


_BINDING_OWNER = ("services", "print_binding.py")
# The one-shot repair of the 2026-09 replay damage and the migrations that run it write archive
# status as DATA REPAIR, at boot, before any binding exists — not as print binding.
_ARCHIVE_STATUS_EXEMPT = frozenset({_BINDING_OWNER, ("services", "foreign_replay_repair.py"), ("core", "database.py")})
# Names DELETED with the process-memory binding (2026-09-25). A reappearance is the leak coming back.
_DELETED_BINDING_NAMES = (
    "_active_prints",
    "_expected_prints",
    "_expected_print_creators",
    "_expected_print_registered_at",
    "_print_ams_mappings",
    "_print_plate_ids",
    "register_expected_print",
    "_evict_stale_expected_prints",
    "_expected_prints_cleanup_task",
    "_expected_prints_cleanup_loop",
    "start_expected_prints_cleanup",
    "stop_expected_prints_cleanup",
    "_EXPECTED_PRINT_TTL_SECONDS",
    "_EXPECTED_PRINT_CLEANUP_INTERVAL",
    "_get_start_plate_id",
    "update_archive_status",
)


def _is_print_archive_attr(node: ast.expr, attr: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id == "PrintArchive"
    )


def _updates_print_archive(expr: ast.expr) -> bool:
    """Is ``expr`` a statement chain built from ``update(PrintArchive)``?"""
    node: ast.expr = expr
    while True:
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "update"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "PrintArchive"
            ):
                return True
            node = func
        elif isinstance(node, ast.Attribute):
            node = node.value
        else:
            return False


def _is_printing(value: ast.expr | None) -> bool:
    return isinstance(value, ast.Constant) and value.value == "printing"


def _scan_archive_live_status(tree: ast.Module) -> list[tuple[str, int]]:
    """Every read or write of an archive's ``printing`` status: the comparison, the ``in_`` list,
    the construction, the Core update and any attribute assignment of the literal."""
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and _is_print_archive_attr(node.left, "status"):
            if any(_is_printing(c) for c in node.comparators):
                hits.append(("PrintArchive.status == 'printing'", node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "in_" and _is_print_archive_attr(func.value, "status"):
                listed = [e for a in node.args if isinstance(a, (ast.List, ast.Tuple, ast.Set)) for e in a.elts]
                if any(_is_printing(e) for e in listed):
                    hits.append(("PrintArchive.status.in_([... 'printing' ...])", node.lineno))
            constructs = isinstance(func, ast.Name) and func.id == "PrintArchive"
            core_update = (
                isinstance(func, ast.Attribute) and func.attr == "values" and _updates_print_archive(func.value)
            )
            if (constructs or core_update) and any(k.arg == "status" and _is_printing(k.value) for k in node.keywords):
                hits.append(
                    ("PrintArchive(status='printing')" if constructs else ".values(status='printing')", node.lineno)
                )
        elif isinstance(node, ast.Assign) and _is_printing(node.value):
            if any(isinstance(t, ast.Attribute) and t.attr == "status" for t in node.targets):
                hits.append(("<row>.status = 'printing'", node.lineno))
    return hits


def _scan_archive_started_at_writes(tree: ast.Module) -> list[tuple[str, int]]:
    """Every write of an archive's ``started_at``: the construction keyword, the Core update, and an
    assignment onto an ``*archive*``-named row."""
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            constructs = isinstance(func, ast.Name) and func.id == "PrintArchive"
            core_update = (
                isinstance(func, ast.Attribute) and func.attr == "values" and _updates_print_archive(func.value)
            )
            if (constructs or core_update) and any(k.arg == "started_at" for k in node.keywords):
                hits.append(("PrintArchive(started_at=...)" if constructs else ".values(started_at=...)", node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "started_at"
                    and isinstance(target.value, ast.Name)
                    and "archive" in target.value.id.lower()
                ):
                    hits.append((f"{target.value.id}.started_at = ...", node.lineno))
    return hits


class TestArchiveBindingOwnership:
    """ONE owner binds a print to its archive — ``services/print_binding.py``. SOURCE pins, like
    their neighbours: a second writer of ``status='printing'`` / ``started_at`` is well-formed code
    that every behaviour test passes, and it is precisely what makes ``started_at IS NULL`` stop
    meaning "never printed" — the adopt's whole precondition — and a name-keyed registry the way
    every print in flight across a restart leaked its archive (RC1, 2026-09-23)."""

    def test_no_archive_printing_status_outside_the_binding_owner(self):
        strays = [
            f"  - {'/'.join(parts)}:{line} {shape}"
            for parts, tree in _app_trees()
            if parts not in _ARCHIVE_STATUS_EXEMPT
            for shape, line in _scan_archive_live_status(tree)
        ]
        if strays:
            pytest.fail(
                "An archive's 'printing' status is read or written outside services/print_binding.py:\n"
                + "\n".join(strays)
                + "\n\nBind a print with print_binding.attach / bind_created, close it with close_archive, "
                "and read the live print with live_print_archive / printers_with_live_print / "
                "count_live_prints."
            )

    def test_no_archive_started_at_write_outside_the_binding_owner(self):
        strays = [
            f"  - {'/'.join(parts)}:{line} {shape}"
            for parts, tree in _app_trees()
            if parts not in _ARCHIVE_STATUS_EXEMPT
            for shape, line in _scan_archive_started_at_writes(tree)
        ]
        if strays:
            pytest.fail(
                "An archive's started_at is written outside services/print_binding.py:\n"
                + "\n".join(strays)
                + "\n\nstarted_at is stamped only when a print is BOUND to the row; that is what lets the "
                "adopt read started_at IS NULL as 'never printed'."
            )

    def test_the_deleted_process_memory_binding_stays_deleted(self):
        import re

        pattern = re.compile(r"\b(" + "|".join(map(re.escape, _DELETED_BINDING_NAMES)) + r")\b")
        strays = [
            f"  - {f.relative_to(BACKEND_DIR).as_posix()}:{number} {match.group(1)}"
            for f in get_python_files(BACKEND_DIR)
            for number, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1)
            if (match := pattern.search(line))
        ]
        if strays:
            pytest.fail(
                "A name of the deleted process-memory print binding reappears in backend/app:\n"
                + "\n".join(strays)
                + "\n\nA print's archive is found by job id in the database (print_binding); a registry "
                "keyed by names is empty after every restart."
            )

    def test_the_owner_still_does_it(self):
        """The liveness half: pins that scan for strays pass on an empty tree too."""
        owner = dict(_app_trees())[_BINDING_OWNER]
        assert {shape for shape, _ in _scan_archive_live_status(owner)} >= {
            "PrintArchive.status == 'printing'",
            ".values(status='printing')",
        }
        assert {shape for shape, _ in _scan_archive_started_at_writes(owner)} == {".values(started_at=...)"}


# --- One archive per print ATTEMPT (2026-09-25) ------------------------------------------------
#
# A unit's ``archive_id`` (and its ``archive`` relationship) names the DONOR — the bytes and names it
# prints FROM, which a retry inherits from its parent — never its own print record. The record is
# ``print_binding.print_archive_of`` (and back, ``unit_of_print_archive``), by job identity.

# Names a queue unit is bound to where this codebase reads its archive link. A receiver named
# otherwise is not scanned; these are the spellings a new reader reaches for.
_QUEUE_UNIT_RECEIVERS = frozenset(
    {"PrintQueueItem", "item", "unit", "fa_item", "queue_item", "i", "it", "retry", "parent"}
)
_QUEUE_UNIT_LINKS = frozenset({"archive_id", "archive"})

# Every reader of a unit's archive link, and the donor question it asks. A reader that wants the
# unit's OWN print (its photo, its outcome, its charge context) belongs on print_archive_of instead.
_DONOR_READERS: dict[tuple[str, str], str] = {
    ("api/routes/print_queue.py", "_enrich_response"): "queue row display: source name, thumbnail, slicer data",
    ("api/routes/print_queue.py", "add_to_queue"): "the new unit's source file name (relay + notification)",
    ("api/routes/print_queue.py", "get_queue_item"): "eager-loads the source for _enrich_response",
    ("api/routes/print_queue.py", "list_queue"): "eager-loads the source for _enrich_response",
    ("api/routes/print_queue.py", "start_queue_item"): "eager-loads the source for the filament pre-check",
    ("api/routes/webhook.py", "webhook_add_to_queue"): "echoes the source archive the caller queued",
    ("api/routes/webhook.py", "webhook_get_queue_status"): "echoes each unit's source archive",
    ("services/archive.py", "ArchiveService.soft_delete_archive"): "delete scope: the units printing FROM it",
    ("services/archive.py", "archive_delete_impact"): "delete guard: the units printing FROM it",
    ("services/eject/manual.py", "_farm_dispatched_names"): "the farm file-name corpus (source names)",
    ("services/farm_correlation.py", "_item_names"): "the names a unit was dispatched under",
    ("services/farm_correlation.py", "resolve_item_donor"): "THE donor resolver: the bytes a unit printed from",
    ("services/farm_stall.py", "_job_name"): "a label for the stalled job (source name)",
    ("services/filament_deficit.py", "_resolve_source_3mf"): "the source 3MF the deficit is computed from",
    ("services/filament_deficit.py", "compute_deficit_for_queue_item"): "eager-loads the source 3MF",
    ("services/print_binding.py", "_adopt"): "the adopt: binds the donor only while it never recorded a print",
    ("services/print_binding.py", "adoptable_dispatch"): "the adopt's pre-check, same rule",
    ("services/print_binding.py", "attach"): "the adopt step of the print-start binding",
    ("services/print_scheduler.py", "PrintScheduler._get_filament_requirements"): "dispatch read: the source file",
    ("services/print_scheduler.py", "PrintScheduler._get_job_name"): "dispatch read: the source name",
    ("services/print_scheduler.py", "PrintScheduler._start_print"): "dispatch read: the bytes to upload",
    ("services/print_scheduler.py", "PrintScheduler.check_queue"): "a log line naming each pending unit's source",
    ("services/usb_storage.py", "_in_use_remote_names"): "file names on the printer's USB a live unit prints from",
    ("services/user_deletion.py", "_destroy_owned_items"): "delete scope: the units printing FROM the user's archives",
    ("services/user_deletion.py", "delete_impact"): "delete forecast over the same scope",
}
# Receivers that share a unit's name but are another model.
_NOT_QUEUE_UNITS: dict[tuple[str, str], str] = {
    ("api/routes/projects.py", "create_bom_item"): "item is a ProjectBOMItem",
    ("api/routes/projects.py", "list_bom_items"): "item is a ProjectBOMItem",
    ("api/routes/projects.py", "update_bom_item"): "item is a ProjectBOMItem",
}

# Attributes that carry a printer job id. Comparing two of them is the job-identity question, which
# has three answers — ``job_identity.same_job``.
_JOB_ID_ATTRS = frozenset({"subtask_id", "dispatch_subtask_id", "dispatch_subtask", "live_subtask"})
_JOB_IDENTITY_OWNERS = frozenset(
    {
        ("services", "job_identity.py"),
        ("services", "print_binding.py"),
        ("services", "farm_correlation.py"),
        ("services", "foreign_replay_repair.py"),
    }
)
# Comparisons outside the owners, each a SQL filter or a question that is not "same job?".
_JOB_ID_COMPARISONS: dict[tuple[str, str], str] = {
    ("services/eject/donor.py", "GateSubtaskArchive.resolve"): "SQL filter: the archive a plate gate's stamp names",
    ("services/eject/manual.py", "_resolve_manual_eject_item"): (
        "the plate gate's stamp → its unit: a SQL filter plus a guard over two farm-minted ids "
        "(eject/* is outside the 2026-09-25 capsule; print_binding.print_archive_of is the adoption path)"
    ),
    ("services/spool_recovery.py", "_resolve_farm_item"): "SQL filter: the unit an echoed job id names",
    ("services/usage_tracker.py", "_resolve_run_item"): "SQL filter: tier 1, the unit the terminal's echo names",
    ("services/print_scheduler.py", "PrintScheduler._watchdog_print_start"): (
        "change detection of the printer's OWN echo across the dispatch (did it flip?), not two jobs compared"
    ),
    ("main.py", "_is_active_archive_stale"): (
        "the downtime reconcile's staleness test — main.py is outside the 2026-09-25 capsule; the reconcile "
        "judge in print_binding replaces it"
    ),
}


def _scopes(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """Every node with the qualified name of the def/class it sits in (``<module>`` at top level)."""
    out: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                walk(child, (*stack, child.name))
            else:
                out.append((".".join(stack) or "<module>", child))
                walk(child, stack)

    walk(tree, ())
    return out


def _scan_queue_unit_archive_reads(py_file: Path) -> list[tuple[str, int]]:
    """``(qualname, line)`` of every READ of a queue unit's ``archive_id`` / ``archive`` link.

    Only modules that name ``PrintQueueItem`` are scanned: a module that never names the model does
    not hold its rows under these receivers."""
    text = py_file.read_text(encoding="utf-8")
    if "PrintQueueItem" not in text:
        return []
    return [
        (qual, node.lineno)
        for qual, node in _scopes(ast.parse(text))
        if isinstance(node, ast.Attribute)
        and node.attr in _QUEUE_UNIT_LINKS
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id in _QUEUE_UNIT_RECEIVERS
    ]


def _names_job_id(node: ast.expr) -> bool:
    """Does this comparison operand spell a job id — the attribute itself, through a method chain
    (``x.subtask_id.strip()``) or an ``or`` default (``(x.subtask_id or "")``)? A call to a FUNCTION
    (``same_job(...)``) is the adopted form and does not count."""
    if isinstance(node, ast.Attribute) and node.attr in _JOB_ID_ATTRS:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _names_job_id(node.func.value)
    if isinstance(node, ast.BoolOp):
        return any(_names_job_id(value) for value in node.values)
    return False


def _scan_job_id_comparisons(tree: ast.Module) -> list[tuple[str, int]]:
    """``(qualname, line)`` of every ``==`` / ``!=`` whose operand is a job id."""
    return [
        (qual, node.lineno)
        for qual, node in _scopes(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
        and any(_names_job_id(operand) for operand in (node.left, *node.comparators))
    ]


class TestPrintRecordResolution:
    """One archive per print ATTEMPT (user ruling 2026-09-25). SOURCE pins, like their neighbours:
    a unit's ``archive_id`` read as its print record is well-formed code every seeded test passes —
    until the unit is a retry, whose ``archive_id`` is its FAILED parent's printed record (the
    first-article approval showed the ancestor's photo; the usage context missed the retry and
    raised ``MultipleResultsFound`` over the shared donor). And job identity has three answers, so a
    second spelling of the comparison is how an id-less echo quietly becomes "the same job"."""

    def test_a_unit_archive_link_is_read_only_as_the_donor(self):
        known = _DONOR_READERS.keys() | _NOT_QUEUE_UNITS.keys()
        strays = [
            f"  - {'/'.join(_relative_parts(py_file))}:{line} ({qual})"
            for py_file in get_python_files(BACKEND_DIR)
            for qual, line in _scan_queue_unit_archive_reads(py_file)
            if ("/".join(_relative_parts(py_file)), qual) not in known
        ]
        if strays:
            pytest.fail(
                "A queue unit's archive_id / archive is read by a function not on the donor allowlist:\n"
                + "\n".join(strays)
                + "\n\nThat link is the DONOR — the bytes the unit prints from, its parent's record for a "
                "retry. The unit's own print is print_binding.print_archive_of(unit) (and back, "
                "unit_of_print_archive). A genuine donor read goes on _DONOR_READERS with its reason."
            )

    def test_the_donor_allowlist_names_only_live_readers(self):
        """The liveness half: an allowlist entry whose reader is gone would excuse the next one."""
        live = {
            ("/".join(_relative_parts(py_file)), qual)
            for py_file in get_python_files(BACKEND_DIR)
            for qual, _ in _scan_queue_unit_archive_reads(py_file)
        }
        stale = sorted((_DONOR_READERS.keys() | _NOT_QUEUE_UNITS.keys()) - live)
        assert not stale, f"allowlisted archive-link readers that no longer read it: {stale}"

    def test_job_identity_is_not_re_spelled(self):
        strays = [
            f"  - {'/'.join(parts)}:{line} ({qual})"
            for parts, tree in _app_trees()
            if parts not in _JOB_IDENTITY_OWNERS
            for qual, line in _scan_job_id_comparisons(tree)
            if ("/".join(parts), qual) not in _JOB_ID_COMPARISONS
        ]
        if strays:
            pytest.fail(
                "A job id is compared with == / != outside the job-identity owners:\n"
                + "\n".join(strays)
                + "\n\nAsk job_identity.same_job (same / other / unknown) and decide what 'unknown' means "
                "at the call site. A SQL filter goes on _JOB_ID_COMPARISONS with its reason."
            )

    def test_the_identity_allowlist_names_only_live_comparisons(self):
        live = {
            ("/".join(parts), qual)
            for parts, tree in _app_trees()
            if parts not in _JOB_IDENTITY_OWNERS
            for qual, _ in _scan_job_id_comparisons(tree)
        }
        stale = sorted(_JOB_ID_COMPARISONS.keys() - live)
        assert not stale, f"allowlisted job-id comparisons that no longer compare: {stale}"

    def test_the_adopted_sites_ask_the_owner(self):
        """The liveness half of the migration: the sites this wave moved really call the owner."""
        from backend.app.services import dispatch_claim, incident_resolution, plate_occupancy_store, production_run

        assert "print_archive_of" in _source_of(production_run.build_run_response)
        assert "same_job" in _source_of(incident_resolution._same_job)  # noqa: SLF001
        assert "same_job" in _source_of(dispatch_claim.judge)
        assert "same_job" in _source_of(plate_occupancy_store._startup_policy)  # noqa: SLF001


def _source_of(obj: object) -> str:
    import inspect

    return inspect.getsource(obj)


def _scan_recovery_incident_constructions(py_file: Path) -> list[int]:
    """Every CONSTRUCTION of ``RecoveryIncident``, however the module was imported.

    AST, not grep, for the usual reason: the type is named in annotations, docstrings
    and comments throughout ``spool_recovery`` — only a ``Call`` counts.
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "RecoveryIncident":
            hits.append(node.lineno)
    return hits


class TestRecoveryIncidentOwnership:
    """ONE factory builds the recovery context — ``spool_recovery._build_incident``.

    A SOURCE pin, like its neighbours above: two call sites open an incident (the
    per-push entry gate and the startup re-entry) and both must derive the SAME facts
    from the same candidate set. They were two 17-field literals, each re-deriving
    ``extruder_side_only`` inline, and a third would be a perfectly well-formed
    behaviour no test would notice — until a restart read one printer's fault
    differently from the push that raised it.
    """

    def test_exactly_one_construction_in_the_app_package(self):
        sites = [
            f"  - {'/'.join(_relative_parts(py_file))}:{line}"
            for py_file in get_python_files(BACKEND_DIR)
            for line in _scan_recovery_incident_constructions(py_file)
        ]
        if len(sites) != 1:
            pytest.fail(
                f"RecoveryIncident must be constructed in exactly one place, found {len(sites)}:\n"
                + "\n".join(sites)
                + "\n\nBuild it through spool_recovery._build_incident: it owns every fact derived "
                "from the wire (the code set, extruder_side_only, retract_failure, the layer, the "
                "job name), so the entry gate and the startup re-entry cannot drift apart."
            )

    def test_that_construction_is_the_factory(self):
        """The liveness half — a pin that counts one site is satisfied by the WRONG one."""
        import inspect

        from backend.app.services import spool_recovery

        source, start = inspect.getsourcelines(spool_recovery._build_incident)  # noqa: SLF001
        (line,) = [
            line for py_file in get_python_files(BACKEND_DIR) for line in _scan_recovery_incident_constructions(py_file)
        ]
        assert start <= line < start + len(source), "the one construction must live inside _build_incident"


# --- The recovery driver's single owners (2026-09-23, the wedge release ladder) -------

_SPOOL_RECOVERY = ("services", "spool_recovery.py")
_AMS_COMMAND = ("services", "ams_command.py")
_PRINTER_INCIDENTS = ("services", "printer_incidents.py")

# The frames a release verb publishes. In ``spool_recovery`` they are published by the
# lever table's own lambdas, by THE reader's pause arms, and by the runout refill /
# path-repair auto-resume lane (a different feature: ``_resume_after_evidence``) — and
# nowhere else, so no second reading of a verb's effect can grow beside the reader.
_RELEASE_FRAMES = {"resume_print", "pause_print", "ams_control", "clean_print_error"}
_RELEASE_FRAME_SCOPES = {"_LEVERS", "_read_after", "_resume_after_evidence"}


def _called(func: ast.expr) -> tuple[str | None, str | None]:
    """``(owner, name)`` of a call's target: ``("printer_incidents", "driver_live")`` for
    ``printer_incidents.driver_live(...)``, ``(None, "driver_live")`` for a bare call."""
    if isinstance(func, ast.Name):
        return None, func.id
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    return None, None


def _scoped_nodes(tree: ast.Module):
    """Every node with its SCOPE: the enclosing class/function names, outermost first,
    and ``("_LEVERS",)`` for a node inside the module-level ``_LEVERS`` table."""
    stack: list[tuple[ast.AST, tuple[str, ...]]] = [(tree, ())]
    while stack:
        node, scope = stack.pop()
        yield node, scope
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                child_scope = (*scope, child.name)
            elif not scope and isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                if any(isinstance(t, ast.Name) and t.id == "_LEVERS" for t in targets):
                    child_scope = ("_LEVERS",)
            stack.append((child, child_scope))


def _app_trees() -> list[tuple[tuple[str, ...], ast.Module]]:
    return [(_relative_parts(f), ast.parse(f.read_text(encoding="utf-8"))) for f in get_python_files(BACKEND_DIR)]


class TestRecoveryDriverOwnership:
    """ONE owner per fact of the recovery driver. SOURCE pins, like their neighbours: every
    failure they catch is a well-formed second copy that every behaviour test passes."""

    def test_no_answer_is_re_judged_from_a_posture_outside_ams_command(self):
        """``ams_command.classify`` is THE reading of a command's answer, and the posture is
        its private key: nothing outside ``ams_command`` may read ``posture()`` or re-derive
        a verdict from a mid-change posture set (``MID_CHANGE_POSTURES`` is deleted) — the
        feeder question is ``spool_recovery._feeder_position``'s."""
        strays: list[str] = []
        for parts, tree in _app_trees():
            if parts == _AMS_COMMAND:
                continue
            posture_imported = any(
                isinstance(n, ast.ImportFrom)
                and (n.module or "").endswith("ams_command")
                and any(alias.name == "posture" for alias in n.names)
                for n in ast.walk(tree)
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    owner, name = _called(node.func)
                    if name == "posture" and (owner == "ams_command" or (owner is None and posture_imported)):
                        strays.append(f"  - {'/'.join(parts)}:{node.lineno} calls ams_command.posture()")
                if (isinstance(node, ast.Name) and node.id == "MID_CHANGE_POSTURES") or (
                    isinstance(node, ast.Attribute) and node.attr == "MID_CHANGE_POSTURES"
                ):
                    strays.append(f"  - {'/'.join(parts)}:{node.lineno} reads MID_CHANGE_POSTURES")
        assert not strays, "An AMS command's answer is re-judged from its posture:\n" + "\n".join(strays)

        # Liveness: the classifier still keys its rows by the posture it owns.
        (tree,) = [t for parts, t in _app_trees() if parts == _AMS_COMMAND]
        assert any(isinstance(n, ast.FunctionDef) and n.name == "posture" for n in ast.walk(tree))
        assert not any(isinstance(n, ast.Name) and n.id == "MID_CHANGE_POSTURES" for n in ast.walk(tree))

    def test_driver_liveness_has_one_store(self):
        """Liveness is ``printer_incidents``'s (``register_driver`` / ``release_driver`` /
        ``driver_live``). The module dict and its reader it replaced exist nowhere, and
        every reader asks the store through its module — never a copied function."""
        strays: list[str] = []
        callers: set[tuple[str, ...]] = set()
        for parts, tree in _app_trees():
            for node in ast.walk(tree):
                for symbol in ("_active_tasks", "has_live_recovery"):
                    if (
                        (isinstance(node, ast.Name) and node.id == symbol)
                        or (isinstance(node, ast.Attribute) and node.attr == symbol)
                        or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol)
                    ):
                        strays.append(f"  - {'/'.join(parts)}:{node.lineno} names {symbol}")
                if isinstance(node, ast.Call):
                    owner, name = _called(node.func)
                    if name != "driver_live":
                        continue
                    if owner == "printer_incidents" or (owner is None and parts == _PRINTER_INCIDENTS):
                        callers.add(parts)
                    else:
                        strays.append(f"  - {'/'.join(parts)}:{node.lineno} calls driver_live() off the store")
        assert not strays, "Driver liveness grew a second spelling:\n" + "\n".join(strays)
        # Liveness: the driver and the pause-stall watch still ask the store.
        assert {_SPOOL_RECOVERY, ("services", "farm_stall.py")} <= callers

    def test_the_step_ledger_has_one_writer(self):
        """``printer_incidents.note_step`` / ``answer_step`` are called ONLY by the evidence
        log's own mutators, so a step is recorded at the send and answered at the read in
        exactly one place."""
        allowed = {("_RecoveryEvidence", "note"): "note_step", ("_RecoveryEvidence", "answer"): "answer_step"}
        found: set[str] = set()
        strays: list[str] = []
        for parts, tree in _app_trees():
            for node, scope in _scoped_nodes(tree):
                if not isinstance(node, ast.Call):
                    continue
                _owner, name = _called(node.func)
                if name not in ("note_step", "answer_step"):
                    continue
                if parts == _SPOOL_RECOVERY and allowed.get(tuple(scope[-2:])) == name:
                    found.add(name)
                else:
                    strays.append(
                        f"  - {'/'.join(parts)}:{node.lineno} calls {name}() in {'.'.join(scope) or '<module>'}"
                    )
        assert not strays, "The recovery step ledger grew a second writer:\n" + "\n".join(strays)
        assert found == {"note_step", "answer_step"}

    def test_the_release_frames_have_one_publisher_per_role(self):
        """In ``spool_recovery`` a release frame (resume / pause / ams_control /
        clean_print_error) is published by the lever table's lambdas, THE reader's pause
        arms (``_read_after``) and the refill / repair auto-resume lane
        (``_resume_after_evidence``) — nowhere else."""
        (tree,) = [t for parts, t in _app_trees() if parts == _SPOOL_RECOVERY]
        seen: set[str] = set()
        strays: list[str] = []
        for node, scope in _scoped_nodes(tree):
            if not isinstance(node, ast.Call):
                continue
            _owner, name = _called(node.func)
            if name not in _RELEASE_FRAMES:
                continue
            home = next((s for s in scope if s in _RELEASE_FRAME_SCOPES), None)
            if home is None:
                strays.append(f"  - spool_recovery:{node.lineno} publishes {name}() in {'.'.join(scope) or '<module>'}")
            else:
                seen.add(home)
        assert not strays, "A release frame is published outside its owners:\n" + "\n".join(strays)
        assert seen == _RELEASE_FRAME_SCOPES

    def test_the_evidence_log_is_built_only_from_the_ledger(self):
        """``_RecoveryEvidence`` is constructed ONLY by ``from_row`` — the one constructor
        both spawn paths use, so a driver can never start from an in-memory log that
        forgot what the incident already sent."""
        strays: list[str] = []
        builds_from_row = False
        for parts, tree in _app_trees():
            for node, scope in _scoped_nodes(tree):
                if not isinstance(node, ast.Call):
                    continue
                _owner, name = _called(node.func)
                if name == "_RecoveryEvidence":
                    strays.append(f"  - {'/'.join(parts)}:{node.lineno} constructs _RecoveryEvidence")
                if (
                    parts == _SPOOL_RECOVERY
                    and name == "cls"
                    and tuple(scope[-2:]) == ("_RecoveryEvidence", "from_row")
                ):
                    builds_from_row = True
        assert not strays, "The evidence log is built outside from_row:\n" + "\n".join(strays)
        assert builds_from_row


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
                "their lane through incident_resolution's table; service_hold.exit is the one "
                "DECLARED exception, named in incident_resolution's module docstring. A second "
                "needs the same declaration."
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
# the plate blow-off, then the upstream per-model snippets), DERIVES the cache key from
# that stack, and hands ``print_scheduler._start_print`` a verdict. Two things are pinned
# here:
#
#   * the three transform entry points — ``chute_prime.rewrite_head``,
#     ``plate_blowoff.insert_blowoff`` and ``threemf_tools.apply_gcode_snippets`` — are called
#     from the seam and nowhere else;
#   * the settings keys that SELECT and SHAPE them are spelled only by the seam that reads
#     them and by the two modules that define the setting (the schema twin, the PUT
#     coercion whitelists).
#
# The failure this catches is a second rewrite site: a perfectly working injection added
# back into the scheduler, a route or a recovery lane. Every behaviour test would pass —
# and the cache key would stop describing the bytes, which is precisely how a kill switch
# turned off still serves chute-primed files out of the cache.
_DISPATCH_SEAM = ("services", "dispatch_file.py")

_DISPATCH_TRANSFORM_CALLS = {"rewrite_head", "insert_blowoff", "apply_gcode_snippets"}

_DISPATCH_SETTING_KEYS = {
    "farm_chute_prime_enabled",
    "farm_plate_blowoff_enabled",
    "farm_plate_blowoff_seconds",
    "gcode_snippets",
}

_DISPATCH_SETTING_SPELLERS = {
    _DISPATCH_SEAM,
    ("schemas", "settings.py"),  # defines the typed field + its update twin
    ("api", "routes", "settings.py"),  # the bool / int coercion whitelists
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

    def test_the_seam_still_owns_every_transform_and_every_key(self):
        """The liveness half: an ownership pin whose owner has moved away polices nothing,
        and the scan above would then pass on a codebase with no seam at all."""
        seam = BACKEND_DIR.joinpath(*_DISPATCH_SEAM)
        assert seam.exists(), f"{'/'.join(_DISPATCH_SEAM)} is gone — the seam moved without this pin"

        found = {symbol for symbol, _ in _scan_dispatch_decisions(seam)}
        expected_symbols = {f"{name}()" for name in _DISPATCH_TRANSFORM_CALLS} | {
            f'"{key}"' for key in _DISPATCH_SETTING_KEYS
        }
        for expected in sorted(expected_symbols):
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

# WHO may hand a measurement to the ledger. The writer itself (it defines both verbs)
# plus the two owners that actually TIME an episode: the eject terminal, which reads
# the sweep's own start echo, and the cooldown prep's retirement, which is the end of
# the cooling episode. A third caller is a duration nobody measured.
_EPISODE_CALLERS = {
    _EPISODE_WRITER,
    ("services", "farm_policy.py"),
    ("services", "eject", "cooldown_prep.py"),
}

# The ledger's two entry points, scanned together. Which one a caller takes is decided
# by whether it holds a session — the terminal rides its own (``record_episode``), the
# sync cooldown retirement cannot (``note_episode``) — but BOTH write the same table,
# so the ownership question is about the pair, never about one spelling.
_EPISODE_ENTRY_POINTS = {"record_episode", "note_episode"}

_FLEET_MODELS = {"PrinterObservationSpan": _SPAN_WRITER, "FarmCycleEpisode": _EPISODE_WRITER}

# The Core statements that WRITE a table, as opposed to selecting from it. Reads are
# deliberately unrestricted — the whole point of recording raw rows is that anything
# may classify them.
_TABLE_WRITE_VERBS = {"insert", "update", "delete"}


def _called_name(func: ast.expr | None) -> str | None:
    """The bare name a call or attribute reference ends in, however it was reached.

    ``insert(...)``, ``sa.insert(...)`` and ``db.add(...)`` all answer with their last
    identifier, which is what every scan below matches on: an import alias must not be
    able to hide a write or a calendar cut from the pin.
    """
    if isinstance(func, ast.Name):
        return func.id
    return getattr(func, "attr", None)


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
        name = _called_name(func)
        if name in _FLEET_MODELS:
            hits.append((name, f"{name}()", node.lineno))
        elif name in _TABLE_WRITE_VERBS and node.args:
            model = _called_name(node.args[0])
            if model in _FLEET_MODELS:
                hits.append((model, f"{name}({model})", node.lineno))
    return hits


def _scan_episode_notes(py_file: Path) -> list[tuple[str, int]]:
    """Every CALL of either ledger entry point, however the module was imported."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node.func)
        if name in _EPISODE_ENTRY_POINTS:
            hits.append((f"{name}()", node.lineno))
    return hits


# --- Fleet metrics: what the READER may not do -------------------------------------

_FLEET_METRICS_PKG = ("services", "fleet_metrics")

# Keywords that turn ``.replace()`` into a MIDNIGHT (or any other wall-clock instant).
# ``microsecond`` and ``tzinfo`` are deliberately absent: truncating sub-second noise
# and dropping a tzinfo are precision, not calendar — the reader's own clock does both.
_MIDNIGHT_KEYWORDS = frozenset({"hour", "minute", "second"})

# Keywords that turn a ``timedelta`` into a STEP across the calendar. A site day is
# 23 h or 25 h across a transition and a week is seven of those, so stepping by a
# fixed-length delta computes a different calendar from the one the buckets are cut
# on — the two agree for most of the year and disagree twice, which is the hardest
# kind of wrong to notice in a series. ``seconds`` and ``minutes`` are absent: those
# are durations, which this package measures all day long.
_CALENDAR_STEP_KEYWORDS = frozenset({"days", "hours", "weeks"})

# Session methods that can only ever mean a database write. No built-in collection
# carries any of these names, so they are flagged on ANY receiver.
_SESSION_MUTATORS = frozenset(
    {"commit", "flush", "merge", "add_all", "bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"}
)

# ``add`` and ``delete`` are also SET and DICT verbs, so they are flagged only on a
# receiver that actually holds a session or names a model. The timeline sweep collects
# its cut points with ``cuts.add(instant)``; a scan that could not tell that from
# ``db.add(row)`` would have to be switched off for the very module the pin most needs
# to cover.
_AMBIGUOUS_MUTATORS = frozenset({"add", "delete"})

# The Core DML constructors, matched as BARE names (``insert(Table)``): a call to a
# plain function by one of these names is SQLAlchemy's, while ``cuts.update(other)``
# is an attribute call on a set and is not.
_DML_CONSTRUCTORS = frozenset({"insert", "update", "delete"})

# How a module spells "this argument is a database handle", for the ambiguous verbs.
_SESSION_HINTS = ("Session", "Connection")
_SESSION_NAMES = frozenset({"db", "session", "conn", "connection"})


def _fleet_metrics_modules() -> list[Path]:
    """Every module of the read-only metrics package, in a stable order."""
    return sorted(get_python_files(BACKEND_DIR.joinpath(*_FLEET_METRICS_PKG)))


def _scan_calendar_cuts(py_file: Path) -> list[tuple[str, int]]:
    """Every midnight this module builds, and every step it takes across the calendar."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "min" and _called_name(node.value) == "time":
            hits.append(("time.min", node.lineno))
            continue
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node.func)
        keywords = {keyword.arg for keyword in node.keywords if keyword.arg is not None}
        if name == "combine":
            hits.append(("datetime.combine(...)", node.lineno))
        elif name == "replace" and keywords & _MIDNIGHT_KEYWORDS:
            hits.append((f".replace({min(keywords & _MIDNIGHT_KEYWORDS)}=...)", node.lineno))
        elif name == "time" and (node.args or keywords):
            # ``time(0)`` — a wall clock being constructed. A bare ``time.time()`` has
            # no arguments and is a clock reading, not a calendar cut.
            hits.append(("time(...)", node.lineno))
        elif name == "timedelta" and keywords & _CALENDAR_STEP_KEYWORDS:
            hits.append((f"timedelta({min(keywords & _CALENDAR_STEP_KEYWORDS)}=...)", node.lineno))
    return hits


def _session_names(tree: ast.Module) -> set[str]:
    """The names in this module that hold a database handle.

    A parameter annotated ``…Session`` or ``…Connection``, plus the fork's own
    conventional spellings — enough to tell ``db.add(row)`` from ``cuts.add(instant)``
    without either allow-listing a module or banning a set.
    """
    names = set(_SESSION_NAMES)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs):
            annotation = ast.unparse(argument.annotation) if argument.annotation is not None else ""
            if any(hint in annotation for hint in _SESSION_HINTS):
                names.add(argument.arg)
    return names


def _scan_database_writes(py_file: Path) -> list[tuple[str, int]]:
    """Every database WRITE this module performs, by session method or DML construct."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    sessions = _session_names(tree)
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _DML_CONSTRUCTORS:
                hits.append((f"{func.id}(...)", node.lineno))
            continue
        if not isinstance(func, ast.Attribute):
            continue
        receiver = _called_name(func.value)
        if func.attr in _SESSION_MUTATORS:
            hits.append((f".{func.attr}()", node.lineno))
        elif func.attr in _AMBIGUOUS_MUTATORS and receiver is not None:
            # A session writes rows; a model or table class builds a DML statement.
            # Anything else named ``add`` or ``delete`` is a collection.
            if receiver in sessions or (receiver[:1].isupper() and func.attr in _DML_CONSTRUCTORS):
                hits.append((f"{receiver}.{func.attr}()", node.lineno))
        elif func.attr in _DML_CONSTRUCTORS and receiver in {"sqlalchemy", "sa"}:
            hits.append((f"{receiver}.{func.attr}(...)", node.lineno))
    return hits


class TestFleetActivityOwnership:
    """One sampler writes the observation log; one leaf writes the episode ledger.

    And the reader that folds both into figures writes neither, and keeps no calendar
    of its own — the two properties that let the definition of *down* change without
    re-recording a single row.
    """

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
                "services/cycle_episodes, through record_episode (the caller's session) or note_episode "
                "(its own), which the two measuring owners call. Reading either table is unrestricted — "
                "recording raw and classifying at read time is the point."
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
                "caller that did not time the episode is recording a guess. Whichever entry point it "
                "would reach for, the answer is the same: measure it, or do not record it."
            )

    def test_the_declared_writers_are_still_there(self):
        """The liveness half. An allowlist whose entries have all moved away silently
        stops policing anything, and both scans would then pass on an empty set."""
        for model, owner in _FLEET_MODELS.items():
            path = BACKEND_DIR.joinpath(*owner)
            assert path.exists(), f"{'/'.join(owner)} is gone — the writer moved without this pin"
            found = {symbol for _, symbol, _ in _scan_fleet_history_writes(path)}
            assert f"{model}()" in found, f"{'/'.join(owner)} no longer constructs {model}"

        # ``cycle_episodes`` DEFINES both verbs rather than calling them, so it is
        # allowlisted but never a hit — both hook modules must be.
        hooks = _EPISODE_CALLERS - {_EPISODE_WRITER}
        callers = {_relative_parts(f) for f in get_python_files(BACKEND_DIR) if _scan_episode_notes(f)}
        assert callers == hooks

        # And each hook takes the entry point its own shape allows: the async terminal
        # rides the session it holds, the sync cooldown retirement opens its own. A hook
        # that swapped them would be the regression this pin exists for — a second
        # connection queueing behind the terminal's transaction on every eject.
        by_module = {
            _relative_parts(f): {symbol for symbol, _ in _scan_episode_notes(f)}
            for f in get_python_files(BACKEND_DIR)
            if _scan_episode_notes(f)
        }
        assert by_module[("services", "farm_policy.py")] == {"record_episode()"}
        assert by_module[("services", "eject", "cooldown_prep.py")] == {"note_episode()"}

    def test_fleet_metrics_makes_no_calendar_cut_of_its_own(self):
        """The metrics reader takes its day, week and hour grid from ONE resolver.

        ``utils/site_time`` owns the site's calendar because a site day is 23 h or 25 h
        across a transition and an ISO week starts at a local midnight. A sweep that
        built a midnight itself, or stepped by ``timedelta(days=1)``, would compute a
        second calendar that agrees with the grid for most of the year and disagrees
        twice — and the series would still add up, just to the wrong days.
        """
        modules = _fleet_metrics_modules()
        assert modules, "services/fleet_metrics/ has no modules — this pin would scan nothing"

        # The liveness half. A package that stopped bucketing altogether would pass a
        # scan for cuts it no longer makes, so the grid must still be coming from the
        # one resolver.
        sources = "\n".join(path.read_text(encoding="utf-8") for path in modules)
        assert "bucket_edges" in sources, (
            "services/fleet_metrics/ no longer uses site_time.bucket_edges — either the grid "
            "moved or this pin is now watching a package that buckets nothing"
        )

        strays = [
            f"  - {'/'.join(_relative_parts(module))}:{line} uses {symbol}"
            for module in modules
            for symbol, line in _scan_calendar_cuts(module)
        ]
        if strays:
            pytest.fail(
                "The metrics reader is cutting its own calendar:\n"
                + "\n".join(strays)
                + "\n\nDay, week and hour boundaries come from utils/site_time (bucket_edges, "
                "day_bounds, site_instant, previous_window) and from nowhere else. A midnight "
                "built here, or a fixed-length step across days/hours/weeks, is a SECOND "
                "calendar: it agrees with the bucket grid for most of the year and disagrees "
                "across every DST transition."
            )

    def test_fleet_metrics_never_writes_to_the_database(self):
        """The reader is read-only, which is what makes read-time classification safe.

        Nothing in the package inserts, updates or deletes: the definition of *down*
        can therefore change — and it will, as the farm grows lanes — without
        invalidating a stored row or needing a rebuild. A write from inside the reader
        would make a figure depend on who read it last.
        """
        modules = _fleet_metrics_modules()
        assert modules, "services/fleet_metrics/ has no modules — this pin would scan nothing"

        # The liveness half. Run the same scanner over a module that certainly DOES
        # write (the observation recorder), so a scanner that silently stopped
        # detecting writes cannot pass this pin by seeing nothing anywhere.
        recorder = BACKEND_DIR.joinpath(*_SPAN_WRITER)
        assert _scan_database_writes(recorder), (
            f"the write scanner found nothing in {'/'.join(_SPAN_WRITER)}, which writes every "
            "sampling tick — the scanner is broken, not the package"
        )

        strays = [
            f"  - {'/'.join(_relative_parts(module))}:{line} calls {symbol}"
            for module in modules
            for symbol, line in _scan_database_writes(module)
        ]
        if strays:
            pytest.fail(
                "The read-only metrics reader writes to the database:\n"
                + "\n".join(strays)
                + "\n\nThis package classifies at READ time so that raw observation rows never "
                "have to be re-recorded when the definition of a class changes. A rollup, a "
                "cache table or a 'just this one flag' written from here takes that property "
                "away: the figures would then depend on when they were last computed."
            )


class TestModuleImports:
    """Tests for module import health."""

    def test_all_modules_importable(self):
        """Verify all Python modules can be imported without errors.

        This catches syntax errors and missing dependencies.

        IMPORTANT: We must NOT ``del sys.modules[name]`` to force a fresh
        import here. ``backend.app.main`` is a stateful module — re-importing
        it builds NEW module-level dicts (_timelapse_baselines,
        _stage22_finish_frames, _bed_cool_waiters, …) and re-runs ``root_logger.
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


# --------------------------------------------------------------------------- #
# The eject line has ONE owner (2026-09-25, services/eject/shop_air)
# --------------------------------------------------------------------------- #
_SHOP_AIR_OWNER = ("services", "eject", "shop_air.py")
_SHOP_AIR_MIGRATION = ("core", "database.py")
# The one-value ruling deleted these: the per-profile threshold, the per-run override, the
# warn floor that policed a hand-typed line, and the two resolvers that read them.
_DELETED_EJECT_LINE_NAMES = frozenset(
    {
        "cooldown_temp_c",
        "cooldown_temp_c_override",
        "farm_cooldown_warn_floor_c",
        "resolve_cooldown_override",
        "_resolve_eject_threshold",
    }
)
# What only the owner may DEFINE: the qualification, the estimate, the line, the predicate.
_SHOP_AIR_OWNED_DEFS = frozenset(
    {"qualify", "estimate", "day_curve", "eject_line_c", "release_ok", "release_limit", "own_air_c", "current_line"}
)
# The readers, and the owner verbs each must reach THROUGH the module (liveness half).
_SHOP_AIR_READERS: dict[tuple[str, ...], frozenset[str]] = {
    ("services", "eject", "monitor.py"): frozenset({"arm_line", "release_ok", "own_air_c"}),
    ("services", "eject", "manual.py"): frozenset({"current_line", "release_ok", "own_air_c"}),
    ("services", "farm_policy.py"): frozenset({"current_line", "release_ok", "own_air_c"}),
    ("main.py",): frozenset({"current_line", "note_reading", "prune"}),
    ("api", "routes", "shop_air.py"): frozenset({"current_line"}),
}
_SHOP_AIR_VERBS = frozenset({"arm_line", "current_line", "release_ok", "release_limit", "own_air_c", "eject_line_c"})


def _is_bare_string_statement(node: ast.AST) -> bool:
    """A docstring (or any bare string statement): prose, where the rule is explained."""
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _scan_deleted_eject_line_names(py_file: Path) -> list[tuple[str, int]]:
    """Every CODE reference to a deleted name — attribute, name, keyword, def, or a string
    (SQL, a settings key) — skipping docstrings, which are where the history is told."""
    import re

    pattern = re.compile(r"\b(" + "|".join(sorted(_DELETED_EJECT_LINE_NAMES)) + r")\b")
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    prose = {id(node.value) for node in ast.walk(tree) if _is_bare_string_statement(node)}
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.keyword):
            name = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.alias):
            name = node.asname or node.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in prose:
            match = pattern.search(node.value)
            name = match.group(1) if match else None
        if name in _DELETED_EJECT_LINE_NAMES:
            hits.append((name, getattr(node, "lineno", 0)))
    return hits


def _shop_air_aliases(tree: ast.Module) -> set[str]:
    """The local names a module binds to the shop_air MODULE (``import … as``, any scope)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "backend.app.services.eject":
            aliases |= {alias.asname or alias.name for alias in node.names if alias.name == "shop_air"}
        elif isinstance(node, ast.Import):
            aliases |= {
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "backend.app.services.eject.shop_air" and alias.asname
            }
    return aliases


def _shop_air_calls(tree: ast.Module) -> tuple[set[str], list[tuple[str, int]]]:
    """(owner verbs reached THROUGH the module, owner verbs imported or called bare)."""
    aliases = _shop_air_aliases(tree)
    through: set[str] = set()
    bare: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            through.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module == "backend.app.services.eject.shop_air":
            bare += [(alias.name, node.lineno) for alias in node.names if alias.name in _SHOP_AIR_VERBS]
    return through, bare


def _scan_shop_air_sample_writes(py_file: Path) -> list[tuple[str, int]]:
    """ORM constructions of a sample, Core writes of its table, and raw SQL writing it."""
    import re

    raw = re.compile(r"(insert\s+into|delete\s+from|update)\s+shop_air_sample\b", re.IGNORECASE)
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node.func)
            if name == "ShopAirSample":
                hits.append(("ShopAirSample()", node.lineno))
            elif name in _TABLE_WRITE_VERBS and node.args and _called_name(node.args[0]) == "ShopAirSample":
                hits.append((f"{name}(ShopAirSample)", node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and raw.search(node.value):
            hits.append(("raw SQL", node.lineno))
    return hits


class TestEjectLineOwnership:
    """ONE eject line: measured shop air + one margin, owned by ``services/eject/shop_air``.

    User ruling 2026-09-25: the per-profile ``cooldown_temp_c`` (33 °C on all three production
    profiles), the per-run override and the warn floor that policed them are deleted — one
    value, measured. These pins keep a second copy from growing back: no code names the
    deleted fields, nothing but the owner derives a line or judges a release, every reader
    reaches the owner by name, and the sample cache has one writer.
    """

    def test_nothing_references_the_deleted_fields_or_setting(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts == _SHOP_AIR_MIGRATION:
                continue  # the expand/contract migration must name the physical columns
            strays += [f"  - {'/'.join(parts)}:{line} {name}" for name, line in _scan_deleted_eject_line_names(py_file)]
        if strays:
            pytest.fail(
                "A deleted eject-line name is back in backend/app:\n"
                + "\n".join(strays)
                + "\n\nThere is ONE eject line — measured shop air + farm_cooldown_margin_c — and it is "
                "services/eject/shop_air's. The profile's cooldown_temp_c, the run override and the warn floor "
                "were deleted by user ruling (2026-09-25); the physical columns stay only for the rollback build."
            )

    def test_the_deletion_scan_is_live(self):
        """The migration still names the physical column it relaxes — proof the scan sees it."""
        found = {name for name, _ in _scan_deleted_eject_line_names(BACKEND_DIR.joinpath(*_SHOP_AIR_MIGRATION))}
        assert {"cooldown_temp_c", "farm_cooldown_warn_floor_c"} <= found

    def test_only_the_owner_defines_the_line_and_the_predicate(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts == _SHOP_AIR_OWNER:
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            strays += [
                f"  - {'/'.join(parts)}:{node.lineno} def {node.name}"
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _SHOP_AIR_OWNED_DEFS
            ]
        if strays:
            pytest.fail(
                "A second definition of the eject line or its release predicate appeared:\n"
                + "\n".join(strays)
                + "\n\nThe qualification, the estimate, the line and release_ok live in services/eject/shop_air "
                "alone; a reader imports them, it never re-derives them."
            )
        owner = ast.parse(BACKEND_DIR.joinpath(*_SHOP_AIR_OWNER).read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(owner) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert defined >= _SHOP_AIR_OWNED_DEFS, "the owner no longer defines what this pin protects"

    def test_every_reader_reaches_the_owner_by_name(self):
        """The watch, the manual and FA gates, the foreign page and the recorder reach the
        line and the predicate through the ``shop_air`` module — never a bare import — so
        every call site names its owner, and each still does (the liveness half)."""
        problems: list[str] = []
        for parts, verbs in _SHOP_AIR_READERS.items():
            tree = ast.parse(BACKEND_DIR.joinpath(*parts).read_text(encoding="utf-8"))
            through, _ = _shop_air_calls(tree)
            missing = verbs - through
            if missing:
                problems.append(f"  - {'/'.join(parts)} no longer reaches shop_air.{sorted(missing)}")
        for py_file in get_python_files(BACKEND_DIR):
            _, bare = _shop_air_calls(ast.parse(py_file.read_text(encoding="utf-8")))
            problems += [f"  - {'/'.join(_relative_parts(py_file))}:{line} imports {name} bare" for name, line in bare]
        if problems:
            pytest.fail("\n".join(problems))

    def test_no_reader_carries_a_temperature_of_its_own(self):
        """The foreign lane lost its threshold end to end; the watch and the prep take the
        LINE (None when shop air is unknown), never a per-plate number."""
        import dataclasses
        import inspect

        from backend.app.services.eject import cooldown_prep, monitor
        from backend.app.services.eject.manual import ForeignFarmFile
        from backend.app.services.farm_correlation import upgrade_to_foreign_auto_eject
        from backend.app.services.plate_occupancy import ForeignAutoEject

        assert [f.name for f in dataclasses.fields(ForeignAutoEject)] == ["profile_id"]
        assert [f.name for f in dataclasses.fields(ForeignFarmFile)] == ["profile_id", "print_name"]
        assert list(inspect.signature(upgrade_to_foreign_auto_eject).parameters) == ["printer_id", "profile_id"]
        assert list(inspect.signature(monitor.watch_bed_and_clear).parameters)[1] == "line_c"
        assert "release_line_c" in inspect.signature(cooldown_prep.begin).parameters
        assert not hasattr(monitor.EjectCooldownMonitor, "active_watch")

    def test_the_sample_cache_has_one_writer(self):
        strays: list[str] = []
        for py_file in get_python_files(BACKEND_DIR):
            parts = _relative_parts(py_file)
            if parts == _SHOP_AIR_OWNER:
                continue
            strays += [f"  - {'/'.join(parts)}:{line} {what}" for what, line in _scan_shop_air_sample_writes(py_file)]
        if strays:
            pytest.fail(
                "shop_air_sample is written outside its owner:\n"
                + "\n".join(strays)
                + "\n\nIt is a derived cache over printer_sensor_history, written ONLY by services/eject/shop_air "
                "(note_reading live, backfill at bootstrap — the migration calls backfill, it never writes rows). "
                "A second writer is a second qualification rule."
            )
        owner = {what for what, _ in _scan_shop_air_sample_writes(BACKEND_DIR.joinpath(*_SHOP_AIR_OWNER))}
        assert {"ShopAirSample()", "insert(ShopAirSample)", "delete(ShopAirSample)"} <= owner
