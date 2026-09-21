"""The print OUTCOME vocabulary — ``services/print_log``'s three buckets.

``print_log_entries.status`` is read by the ``/archives/stats`` counters and by
the per-archive run tally, and before the buckets existed each of them spelled
the sets itself — which is how the tally came to count ``failed`` while stats
counted ``failed`` and ``aborted``. These pins hold the fold total, disjoint and
closed, and pin the one place a DIFFERENT status set has to stay reconcilable
with it: the statuses an operator may assign by hand.
"""

import ast
from pathlib import Path

import pytest

from backend.app.services.print_log import (
    CANCELLED_STATUSES,
    COMPLETED_STATUS,
    FAILED_STATUSES,
    outcome_bucket,
)

_BACKEND_APP = Path(__file__).parents[3] / "app"

#: The vocabulary ``print_log_entries.status`` is written in.
_OUTCOME_LITERALS = frozenset({"completed", "failed", "aborted", "stopped", "cancelled", "skipped"})

#: The module that owns the fold, and is therefore the one place the words appear.
_OUTCOME_OWNER: tuple[str, ...] = ("services", "print_log.py")

#: Modules allowed to declare an outcome set of their OWN, because each answers a
#: different question from "how did this print turn out", and folding it onto the
#: buckets would change a number an operator reads:
#:
#:   * ``api/routes/print_log.py`` — ``_STATUS_KEYS``, which statuses an operator may
#:     ASSIGN to a row by hand. A subset of the buckets (no ``aborted``), pinned
#:     against them by :class:`TestOperatorAssignableStatusesStayReconcilable`.
#:   * ``api/routes/projects.py`` — ``_FAILURE_STATUSES``, "did this run fail to
#:     deliver a part", deliberately WIDER: a cancelled run delivers none either.
#:
#: Nothing else may own one. A module that does not touch ``PrintLogEntry`` is out
#: of scope entirely rather than allow-listed — ``farm_policy._TERMINAL_RUN_OUTCOMES``
#: spells two of these words against ``PrintQueueItem.status``, a different column
#: with its own vocabulary, and is no business of this rule.
_OWN_SET_ALLOWED: frozenset[tuple[str, ...]] = frozenset(
    {
        ("api", "routes", "print_log.py"),
        ("api", "routes", "projects.py"),
    }
)


def _is_status_attribute(node: ast.expr) -> bool:
    """Is this ``PrintLogEntry.status``?"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "status"
        and isinstance(node.value, ast.Name)
        and node.value.id == "PrintLogEntry"
    )


def _outcome_literals_in(node: ast.expr) -> set[str]:
    """The outcome words this node spells DIRECTLY — a named constant spells none."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value} & _OUTCOME_LITERALS
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        } & _OUTCOME_LITERALS
    return set()


def scan_status_comparisons(source: str) -> list[tuple[int, str]]:
    """Every ``PrintLogEntry.status`` compared against a BARE outcome literal.

    AST rather than grep: the words are named in prose and in docstrings all over
    this tree (they are what the rules are about), so pinning the text would make
    the rule unwritable. ``==``, ``!=`` and ``.in_(...)`` are the three shapes the
    column is actually queried with.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare) and _is_status_attribute(node.left):
            for comparator in node.comparators:
                found = _outcome_literals_in(comparator)
                if found:
                    hits.append((node.lineno, ", ".join(sorted(found))))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "in_"
            and _is_status_attribute(node.func.value)
        ):
            for argument in node.args:
                found = _outcome_literals_in(argument)
                if found:
                    hits.append((node.lineno, ", ".join(sorted(found))))
    return hits


def scan_outcome_set_constants(source: str) -> list[tuple[int, str]]:
    """Every name bound to a collection of NOTHING BUT outcome words.

    The regrowth shape the comparison scan alone would miss: re-spelling the set
    as a fresh constant and then comparing against that name.
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.args:
            if value.func.id in ("frozenset", "set", "tuple", "list"):
                value = value.args[0]
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            continue
        strings = [
            element.value
            for element in value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        # Every element a string, at least two of them, all of them outcome words.
        if len(strings) < 2 or len(strings) != len(value.elts) or not set(strings) <= _OUTCOME_LITERALS:
            continue
        names = ", ".join(target.id for target in node.targets if isinstance(target, ast.Name))
        hits.append((node.lineno, names or "<unnamed>"))
    return hits


class TestOutcomeBucket:
    @pytest.mark.parametrize(
        ("status", "bucket"),
        [
            ("completed", "completed"),
            ("failed", "failed"),
            ("aborted", "failed"),
            ("stopped", "cancelled"),
            ("cancelled", "cancelled"),
            ("skipped", "cancelled"),
        ],
    )
    def test_every_status_in_play_has_its_bucket(self, status: str, bucket: str):
        assert outcome_bucket(status) == bucket

    @pytest.mark.parametrize("status", [None, "", "printing", "Completed", "unknown"])
    def test_anything_else_is_no_bucket(self, status: str | None):
        """An unregistered status is NOT folded into a bucket it never joined.

        Case matters too: the column stores lowercase, and a tally that absorbed
        ``"Completed"`` as a success would be a number nobody could check.
        """
        assert outcome_bucket(status) is None

    def test_the_three_sets_are_disjoint(self):
        """The fold has ONE answer per status, so no status may sit in two sets."""
        completed = {COMPLETED_STATUS}
        failed = set(FAILED_STATUSES)
        cancelled = set(CANCELLED_STATUSES)
        assert completed & failed == set()
        assert completed & cancelled == set()
        assert failed & cancelled == set()

    def test_every_member_of_every_set_round_trips(self):
        """Membership and the classifier cannot drift apart."""
        assert outcome_bucket(COMPLETED_STATUS) == "completed"
        assert {outcome_bucket(status) for status in FAILED_STATUSES} == {"failed"}
        assert {outcome_bucket(status) for status in CANCELLED_STATUSES} == {"cancelled"}


class TestOperatorAssignableStatusesStayReconcilable:
    """``routes/print_log._STATUS_KEYS`` answers a DIFFERENT question — and must still fold.

    It is the set an operator may PATCH onto a row by hand; ``aborted`` is
    deliberately absent from it (the farm writes that one, a human does not). But
    every status a human CAN assign has to land in a bucket, or reclassifying a
    print by hand would drop it out of the stats it was reclassified for.
    """

    def test_every_operator_assignable_status_is_in_a_bucket(self):
        from backend.app.api.routes.print_log import _STATUS_KEYS

        assert {status: outcome_bucket(status) for status in sorted(_STATUS_KEYS)} == {
            "cancelled": "cancelled",
            "completed": "completed",
            "failed": "failed",
            "skipped": "cancelled",
            "stopped": "cancelled",
        }


class TestNoBareOutcomeLiterals:
    """The ownership pin: one module spells these words against ``PrintLogEntry.status``.

    Constants alone do not make an origin — the tally and the stats counters both
    had their own copy of "what counts as failed", disagreed about ``aborted`` for
    months, and neither was wrong on its own line. This scan is what makes the
    second copy fail CI instead of being found by the number it moves.
    """

    def test_no_module_compares_status_against_a_bare_literal(self):
        strays: list[str] = []
        for py_file in sorted(_BACKEND_APP.rglob("*.py")):
            parts = py_file.relative_to(_BACKEND_APP).parts
            if parts == _OUTCOME_OWNER:
                continue
            for line, words in scan_status_comparisons(py_file.read_text(encoding="utf-8")):
                strays.append(f"  - {'/'.join(parts)}:{line} compares status against {words}")

        if strays:
            pytest.fail(
                "Something outside services/print_log.py spells the outcome vocabulary at a "
                "PrintLogEntry.status comparison:\n"
                + "\n".join(strays)
                + "\n\nImport COMPLETED_STATUS / FAILED_STATUSES / CANCELLED_STATUSES instead. A "
                "second copy of the fold drifts silently: the per-archive run tally counted "
                "'failed' while /archives/stats counted 'failed' and 'aborted', so one reprint "
                "was a failure on the card and not in the stats."
            )

    def test_no_module_declares_an_outcome_set_of_its_own(self):
        """...and none re-spells the set as a fresh constant to compare against.

        Scoped to modules that actually touch ``PrintLogEntry``: these six words
        also name other columns' vocabularies (``PrintQueueItem.status`` among
        them), and this rule is about the PRINT-LOG fold, not about the words.
        """
        strays: list[str] = []
        for py_file in sorted(_BACKEND_APP.rglob("*.py")):
            parts = py_file.relative_to(_BACKEND_APP).parts
            if parts == _OUTCOME_OWNER or parts in _OWN_SET_ALLOWED:
                continue
            source = py_file.read_text(encoding="utf-8")
            if "PrintLogEntry" not in source:
                continue
            for line, name in scan_outcome_set_constants(source):
                strays.append(f"  - {'/'.join(parts)}:{line} declares {name}")

        if strays:
            pytest.fail(
                "A module that reads print-log outcomes declared its own outcome set:\n"
                + "\n".join(strays)
                + "\n\nImport the three sets from services/print_log.py. If it genuinely answers a "
                "DIFFERENT question — as routes/print_log._STATUS_KEYS and "
                "routes/projects._FAILURE_STATUSES do — say which, in a comment, and add it to "
                "_OWN_SET_ALLOWED with that reason."
            )

    def test_the_scanners_catch_what_they_are_supposed_to_catch(self):
        """The liveness half: a pin that cannot fail polices nothing.

        Both scanners are pointed at source that IS the violation, so a refactor
        that quietly breaks the AST shapes above turns the two rules green by
        accident and this test red on purpose.
        """
        comparison_source = (
            "q.where(PrintLogEntry.status == 'completed')\nq.where(PrintLogEntry.status.in_(['failed', 'aborted']))\n"
        )
        assert [words for _, words in scan_status_comparisons(comparison_source)] == [
            "completed",
            "aborted, failed",
        ]

        # A named constant is not a bare literal — the whole point of the fix.
        assert scan_status_comparisons("q.where(PrintLogEntry.status == COMPLETED_STATUS)") == []
        assert scan_status_comparisons("q.where(PrintLogEntry.status.in_(FAILED_STATUSES))") == []
        # ...and another column's comparison is not this rule's business.
        assert scan_status_comparisons("q.where(PrintQueueItem.status == 'completed')") == []

        assert scan_outcome_set_constants("_MINE = ('failed', 'aborted')") == [(1, "_MINE")]
        assert scan_outcome_set_constants("_MINE = frozenset({'stopped', 'skipped'})") == [(1, "_MINE")]
        # A set that is not purely outcome words names something else.
        assert scan_outcome_set_constants("_MINE = ('failed', 'gremlin')") == []

    def test_the_owner_still_owns_the_vocabulary(self):
        """The other liveness half: the scan's exemption must still name a real owner."""
        owner = _BACKEND_APP.joinpath(*_OUTCOME_OWNER)
        assert owner.exists(), "services/print_log.py is gone — the fold moved without this pin"

        source = owner.read_text(encoding="utf-8")
        for name in ("COMPLETED_STATUS", "FAILED_STATUSES", "CANCELLED_STATUSES", "def outcome_bucket"):
            assert name in source, f"services/print_log.py no longer declares {name}"
