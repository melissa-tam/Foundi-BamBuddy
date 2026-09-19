"""``dispatch_claim`` — the start-watchdog registry and the claim judge.

The module exists because ``farm_stall`` used to infer one task's liveness from a
clock: a ``printing`` claim had to be 600 s old before the dead-claim watch would look
at it, so that the start watchdog was "ALWAYS the first responder". The watchdog exits
on any active state and dies with every restart, so the claims with NO watchdog — the
ones nothing else on the farm can retire — waited ~12 minutes while the UI said
"printing" over a demonstrably idle printer.

Two things are pinned here, and they are the two halves of the replacement:

* the REGISTRY answers liveness honestly — a finished, cancelled or superseded task is
  not a live watchdog, and popping is scoped to the task that registered;
* the JUDGE is a table. Every guard that used to be a paragraph in a docstring is a row
  that can be asked about, including the three double-dispatch guards, which must NEVER
  yield ``dead`` — the failure mode of a wrong release is a print onto an occupied plate.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.services import dispatch_claim
from backend.app.services.dispatch_claim import (
    DISPATCH_START_BUDGET_S,
    ClaimEvidence,
    has_live_start_watchdog,
    judge,
    register_start_watchdog,
)

pytestmark = pytest.mark.asyncio


def _evidence(**overrides) -> ClaimEvidence:
    """A claim that is DEAD on every axis, so each case names only its one difference."""
    base = {
        "connected": True,
        "state_fresh": True,
        "live_state": "IDLE",
        "offline_stalled": False,
        "archive_printing": False,
        "dispatch_subtask": "dispatch-1",
        "live_subtask": "some-other-job",
        "claim_age_s": DISPATCH_START_BUDGET_S + 5.0,
        "watchdog_live": False,
        "recovery_acting": False,
    }
    base.update(overrides)
    return ClaimEvidence(**base)


class TestTheVerdictTable:
    """One row per verdict, over the same baseline — so each assertion isolates the
    ONE fact that produced it."""

    async def test_the_baseline_is_dead(self):
        """Connected, fresh, idle, no archive, a mismatched echo, past the budget, and
        nobody owning it. That constellation IS the 001-H2S item-1010 shape."""
        assert judge(_evidence()) == "dead"

    @pytest.mark.parametrize(
        "field",
        ["connected", "state_fresh", "offline_stalled"],
    )
    async def test_an_unreadable_wire_is_offline(self, field):
        """Disconnected, STALE (a cached snapshot nobody is refreshing) and
        already-flagged all mean the same thing: this decision cannot be made here."""
        # ``offline_stalled`` is the one whose UNREADABLE polarity is True.
        assert judge(_evidence(**{field: field == "offline_stalled"})) == "offline"

    @pytest.mark.parametrize("live", ["PREPARE", "SLICING", "RUNNING", "PAUSE"])
    async def test_an_active_state_is_started(self, live):
        """PAUSE is ACTIVE on purpose: a native-vision trip pauses at print start with
        the plate occupied, and releasing that unit would re-dispatch onto it."""
        assert judge(_evidence(live_state=live)) == "started"

    async def test_no_state_at_all_is_started(self):
        """An absent state is not evidence of absence — it fails closed."""
        assert judge(_evidence(live_state="")) == "started"

    async def test_a_printing_archive_is_started(self):
        """Hard disjointness with ``main.reconcile_stale_active_prints``: the two
        reconcilers can never both act on one printer."""
        assert judge(_evidence(archive_printing=True)) == "started"

    async def test_a_matching_echo_is_started(self):
        assert judge(_evidence(live_subtask="dispatch-1")) == "started"

    async def test_a_null_dispatch_id_is_no_defence(self):
        """The id is CORROBORATION, never a precondition: requiring one would strand
        exactly the rows that need this most."""
        assert judge(_evidence(dispatch_subtask="")) == "dead"

    async def test_a_live_watchdog_owns_the_claim_at_any_age(self):
        """THE row that replaced the 600 s guess. Ownership is asked, so a watchdog
        genuinely still working keeps its full budget however old the claim is."""
        assert judge(_evidence(watchdog_live=True, claim_age_s=20 * 60.0)) == "watchdog_owns"

    async def test_inside_the_start_budget_is_too_fresh(self):
        assert judge(_evidence(claim_age_s=DISPATCH_START_BUDGET_S - 1.0)) == "too_fresh"

    async def test_an_unknowable_age_is_too_fresh(self):
        """No ``started_at``: the age is unknowable, and an unknowable age is not
        evidence."""
        assert judge(_evidence(claim_age_s=None)) == "too_fresh"

    async def test_an_acting_recovery_holds_the_claim(self):
        assert judge(_evidence(recovery_acting=True)) == "recovery_acting"

    async def test_the_budget_boundary_releases(self):
        """The floor is ``<``, so a claim exactly at the budget is past it."""
        assert judge(_evidence(claim_age_s=DISPATCH_START_BUDGET_S)) == "dead"


class TestTheDoubleDispatchGuardsOutrankEverything:
    """The three 'the print landed' guards must survive every other fact, because the
    cost of a wrong release is a print onto an occupied plate — not a stuck row."""

    @pytest.mark.parametrize(
        "started_by",
        [
            {"live_state": "PAUSE"},
            {"live_subtask": "dispatch-1"},
            {"archive_printing": True},
        ],
    )
    @pytest.mark.parametrize(
        "otherwise_dead",
        [
            {"claim_age_s": 24 * 3600.0},
            {"watchdog_live": False, "recovery_acting": False},
            {"claim_age_s": 24 * 3600.0, "recovery_acting": True},
        ],
    )
    async def test_a_started_print_is_never_released(self, started_by, otherwise_dead):
        assert judge(_evidence(**started_by, **otherwise_dead)) == "started"


class TestTheStartWatchdogRegistry:
    async def test_an_unregistered_item_has_no_watchdog(self):
        assert has_live_start_watchdog(4242) is False

    async def test_a_running_task_is_live_and_a_finished_one_is_not(self):
        gate = asyncio.Event()

        async def _watch():
            await gate.wait()

        task = asyncio.create_task(_watch())
        register_start_watchdog(7, task)
        await asyncio.sleep(0)
        assert has_live_start_watchdog(7) is True

        gate.set()
        await task
        await asyncio.sleep(0)  # let the done-callback run

        assert has_live_start_watchdog(7) is False
        assert 7 not in dispatch_claim._start_watchdogs, "the done-callback must pop its own slot"

    async def test_a_cancelled_task_is_not_live(self):
        """A cancelled loop must not leave a registry entry that silences the
        dead-claim watch forever."""

        async def _watch():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_watch())
        register_start_watchdog(8, task)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert has_live_start_watchdog(8) is False
        assert 8 not in dispatch_claim._start_watchdogs

    async def test_an_older_task_finishing_does_not_unregister_a_newer_one(self):
        """A re-dispatch registers a NEW watchdog for the same item id. The old task's
        done-callback must not then report the live one as gone."""
        gate = asyncio.Event()

        async def _done_now():
            return None

        async def _still_watching():
            await gate.wait()

        old = asyncio.create_task(_done_now())
        register_start_watchdog(9, old)
        new = asyncio.create_task(_still_watching())
        register_start_watchdog(9, new)

        await old
        await asyncio.sleep(0)

        assert has_live_start_watchdog(9) is True
        gate.set()
        await new


class TestTheModuleIsALeaf:
    """Both consumers import it at MODULE level, because there is no cycle to dodge.

    Source-text assertions on purpose: an ``import`` that only runs inside a function
    body is invisible to ``sys.modules`` inspection, and that is exactly the shape being
    banned — a call-time import is what hides a dependency instead of breaking it.
    (Same idiom as ``test_import_graph.py``.)
    """

    async def test_it_imports_no_farm_service(self):
        """``plate_occupancy`` is the ONE exception, and it is the authority that owns
        ``ACTIVE_PRINT_STATES`` — a stdlib-only sync core, so importing the set from its
        origin costs no edge and prevents a second spelling."""
        import inspect

        imports = [
            line.strip()
            for line in inspect.getsource(dispatch_claim).splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        service_imports = [line for line in imports if "backend.app.services" in line]
        assert service_imports == ["from backend.app.services.plate_occupancy import ACTIVE_PRINT_STATES"]

    async def test_both_consumers_import_it_at_module_level(self):
        import inspect

        from backend.app.services import farm_stall, print_scheduler

        for module in (farm_stall, print_scheduler):
            top_level = [
                line
                for line in inspect.getsource(module).splitlines()
                if line.startswith("from backend.app.services.dispatch_claim import")
            ]
            assert top_level, f"{module.__name__} must import dispatch_claim at module level"


class TestTheBudgetHasOneOrigin:
    async def test_the_watchdog_phase_a_default_reads_the_module_constant(self):
        """Two spellings of "how long may a start take" is how the two lanes would come
        to disagree about a claim neither of them owns."""
        import inspect

        from backend.app.services.print_scheduler import PrintScheduler

        sig = inspect.signature(PrintScheduler._watchdog_print_start)
        assert sig.parameters["timeout"].default == DISPATCH_START_BUDGET_S
