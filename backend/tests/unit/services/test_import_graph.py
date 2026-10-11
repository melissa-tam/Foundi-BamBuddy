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
# ``ams_command``, which imports ``printer_manager`` to publish) into ``refill_verdict``.
#
# The toolhead FEED STATE (``feed_state``, 2026-10-10) joins them: the MQTT client steps it once per
# push and every reader above the registry reads its frozen snapshot, so it may reach only the two
# pure wire leaves it reads the frame through — never the client, never the incident store (the
# client's ``farm_acting`` answer is INJECTED by the registry, so the client imports nothing above
# itself either).

_LEAVES = ("refill_verdict", "live_reading", "feed_state")
_ABOVE_THE_REGISTRY = ("printer_manager", "spool_recovery", "ams_command", "print_reconcile", "job_terminal")
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
        """The ``farm_acting`` provider is injected by the registry: ``bambu_mqtt`` never reaches
        the incident store or anything that imports the registry."""
        lines = _import_lines("bambu_mqtt")
        above = (*_ABOVE_THE_REGISTRY, "printer_incidents")
        assert not [line for line in lines if any(name in line for name in above)]
