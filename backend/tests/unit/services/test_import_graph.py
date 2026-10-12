"""The taxonomy is a LEAF: ``hms_errors`` imports neither of its consumers.

``print_scheduler`` and ``spool_recovery`` both need the same question answered —
"what ACTIONABLE fault is standing on this printer's wire right now" — and
``spool_recovery`` imports ``print_scheduler``. While ``live_candidates`` lived in
``spool_recovery`` the scheduler could only reach it through a function-level
import inside two function bodies, which hides a cycle rather than breaking it.
Moving the five pure taxonomy readers into ``hms_errors`` (their only dependency)
makes the edge one-way, and these pins are what stops it growing back.

Source-text assertions on purpose: an ``import`` statement that only runs inside a
function body is invisible to ``sys.modules`` inspection, which is exactly the
shape being banned.
"""

import importlib
import inspect
import subprocess
import sys
from pathlib import Path

from backend.app.services import hms_errors, print_scheduler


class TestTaxonomyIsALeaf:
    def test_hms_errors_imports_neither_consumer(self):
        """Prose may NAME them (the moved block's comment records why it moved);
        an import statement may not — that is the edge."""
        imports = [
            line.strip()
            for line in inspect.getsource(hms_errors).splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "spool_recovery" in line or "print_scheduler" in line]

    def test_the_scheduler_does_not_reach_into_spool_recovery_for_the_gate(self):
        """The two function-level imports (``_busy_cause`` and ``_is_printer_idle``)
        are gone — not relocated to another body."""
        src = inspect.getsource(print_scheduler)
        assert "from backend.app.services.spool_recovery import live_candidates" not in src


class TestTheMovedNamesAreImportable:
    def test_all_five_names_live_in_hms_errors(self):
        from backend.app.services.hms_errors import (  # noqa: F401
            ACTIONABLE_CLASSES,
            FaultCandidate,
            candidate_fingerprint,
            fault_tokens,
            live_candidates,
        )

        assert live_candidates(None) == frozenset()


# --- The refill verdict is a LEAF (K7, 2026-10-10) ----------------------------------------------------
#
# Two readers need "what would a Resume do about this empty toolhead": the resume verb and the per-push
# detectors (``spool_recovery``) and the status frame's ``toolhead.refill_reason`` (``printer_manager``,
# whose own broadcast — ``_broadcast_status_change``, used by maintenance mode — builds the frame). And
# ``spool_recovery`` imports ``printer_manager``. So the verdict lives in a leaf that reaches nothing
# above the client registry: the client's peaks come in as an argument, and the two pure readers it
# composed from modules ABOVE the registry moved down with it — ``is_fresh`` (from ``print_reconcile``,
# which imports ``job_terminal`` → ``printer_manager``) into ``live_reading``, and ``ran`` (from
# ``ams_command``, which imported ``printer_manager`` to publish) into ``refill_verdict`` — and on
# 2026-10-11 from there into the ``command_outcome`` leaf.
#
# The toolhead FEED STATE (``feed_state``, 2026-10-10) joins them: the MQTT client steps it once per
# push and every reader above the registry reads its frozen snapshot, so it may reach only the two
# pure wire leaves it reads the frame through — never the client, never the incident store (and the
# client imports nothing above itself either: whose load landed is the record's, ``printer_action``).
#
# The printer-event hub (``printer_events``, 2026-10-11) joins them: the registry publishes the
# client's facts on it and every lane subscribes through the composition root, so the hub itself
# imports the stdlib alone at runtime — its event fields name the producers' types under
# ``TYPE_CHECKING`` only.

# The record's outcome tables (``command_outcome``, 2026-10-11) join them: every command family's
# answer is a pure reading of the action's facts and one event, so the record above the registry
# (``printer_action``) and the leaves below it read ONE table.
_LEAVES = ("refill_verdict", "live_reading", "feed_state", "printer_events", "command_outcome")
_ABOVE_THE_REGISTRY = ("printer_manager", "spool_recovery", "printer_action", "print_reconcile", "job_terminal")
_REPO = Path(__file__).resolve().parents[4]


def _import_lines(module_name: str) -> list[str]:
    assert importlib.util.find_spec(f"backend.app.services.{module_name}") is not None, f"no {module_name} module"
    module = importlib.import_module(f"backend.app.services.{module_name}")
    return [
        line.strip() for line in inspect.getsource(module).splitlines() if line.strip().startswith(("import ", "from "))
    ]


class TestTheRefillVerdictIsALeaf:
    def test_the_leaves_import_nothing_above_the_registry(self):
        """Source text, so a function-level import is caught too."""
        for leaf in _LEAVES:
            lines = _import_lines(leaf)
            assert not [line for line in lines if any(name in line for name in _ABOVE_THE_REGISTRY)], leaf

    def test_importing_the_leaves_never_loads_the_registry(self):
        """Transitively, at import: a fresh interpreter that imports the leaves has not loaded the
        registry (nor the recovery driver) — the edge ``printer_manager`` → leaf closes no cycle."""
        for leaf in _LEAVES:
            assert importlib.util.find_spec(f"backend.app.services.{leaf}") is not None, f"no {leaf} module"
        probe = (
            "import sys\n"
            f"import {', '.join(f'backend.app.services.{leaf}' for leaf in _LEAVES)}\n"
            "loaded = sorted(m for m in ('backend.app.services.printer_manager', 'backend.app.services.spool_recovery')"
            " if m in sys.modules)\n"
            "print(loaded)\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], cwd=_REPO, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[]", result.stdout

    def test_the_registry_never_imports_the_recovery_driver(self):
        """``printer_manager`` reads the verdict from the leaf — never from ``spool_recovery``."""
        lines = _import_lines("printer_manager")
        assert not [line for line in lines if "spool_recovery" in line]


class TestTheHoldChipSitsBelowTheRegistry:
    """``services/hold_chip`` (2026-10-11) builds the card's hold chip on every status broadcast, called
    from INSIDE the registry (``printer_manager.printer_state_to_dict`` and both ``/status`` branches).
    It reads the store, the rule table and the verdict leaves — never the registry or the recovery driver,
    so the edge ``printer_manager`` → ``hold_chip`` closes no cycle."""

    def test_it_imports_nothing_above_the_registry(self):
        lines = _import_lines("hold_chip")
        assert not [line for line in lines if any(name in line for name in _ABOVE_THE_REGISTRY)]

    def test_importing_it_never_loads_the_registry(self):
        probe = (
            "import sys\n"
            "import backend.app.services.hold_chip\n"
            "loaded = sorted(m for m in ('backend.app.services.printer_manager', 'backend.app.services.spool_recovery',"
            " 'backend.app.services.printer_action') if m in sys.modules)\n"
            "print(loaded)\n"
        )
        result = subprocess.run([sys.executable, "-c", probe], cwd=_REPO, capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[]", result.stdout


_FEED_STATE_IMPORTS = frozenset({"backend.app.services.tray_fields", "backend.app.services.hms_errors"})


class TestTheFeedStateIsALeaf:
    def test_it_imports_the_two_wire_leaves_and_the_stdlib_only(self):
        """Pure, sync and total, on the MQTT thread: nothing farm-side but ``tray_fields`` and
        ``hms_errors`` (source text, so a function-level import is caught too)."""
        farm_side = set()
        for line in _import_lines("feed_state"):
            if "backend." not in line:
                continue
            if line.startswith("from backend.app.services import "):
                names = line.removeprefix("from backend.app.services import ").split(",")
                farm_side |= {f"backend.app.services.{name.strip()}" for name in names}
            else:
                farm_side.add(line.split()[1])
        assert farm_side == _FEED_STATE_IMPORTS

    def test_the_client_imports_nothing_above_itself(self):
        """``bambu_mqtt`` never reaches the incident store or anything that imports the registry (its
        transport facts reach the farm through the registry's injected hand-over callbacks)."""
        lines = _import_lines("bambu_mqtt")
        above = (*_ABOVE_THE_REGISTRY, "printer_incidents")
        assert not [line for line in lines if any(name in line for name in above)]


class TestThePrinterEventHubIsALeaf:
    def test_it_imports_the_stdlib_alone_at_runtime(self):
        """Every farm-side import sits under ``if TYPE_CHECKING:`` (AST, so a function-level import
        is caught too): importing the hub loads no producer and no subscriber."""
        import ast

        module = importlib.import_module("backend.app.services.printer_events")
        tree = ast.parse(inspect.getsource(module))
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                guarded |= {id(child) for child in ast.walk(node)}
        runtime = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and id(node) not in guarded
            and "backend" in ast.unparse(node)
        ]
        assert runtime == []
