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

import inspect

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
