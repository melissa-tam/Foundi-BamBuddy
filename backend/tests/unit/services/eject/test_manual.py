"""Manual "Eject plate" service — one verdict per call, pinned per outcome.

Since the WS4 rewrite ``manual_eject`` RETURNS an
:class:`~backend.app.services.eject.manual.EjectVerdict` instead of raising one of three
exception classes, so every test here asserts a verdict's ``outcome`` and the fields that
outcome carries. Two things that used to be errors are no longer errors at all:

* the foreign-plate confirm prompt is ``needs_input`` — the eject ASKING the operator for
  the part height and the sweep profile, which is the feature, not a failure; and
* a farm-known unit this lane cannot sweep from its own record (no eject profile, a gate
  naming a unit that is not the printer's latest start) reaches the same prompt instead of
  the old hard 409 dead end.

Plate, pending eject and armed watch are ONE record held by ``plate_occupancy`` (the
2026-08-30 cut-over), so state is driven through the authority's transitions: the gate is
seeded with ``hydrate_plate`` (or raised by the service's own ``declare_occupied``), "a
cooldown watch is armed for unit N" is "the plate's policy IS ``CooldownEject(N)``", and
an eject in flight is a claimed ``PendingEject`` — a HYDRATED one deliberately does NOT
refuse an operator.

Donor TIER rules live in ``test_donor.py``; what is pinned here is the FLOW — which
outcome each state produces, and in what order the preconditions run.
"""

import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services.eject import donor as donor_mod, manual, remote as eject_remote
from backend.app.services.plate_occupancy import (
    CooldownEject,
    EscalationOnly,
    Evidence,
    FirstArticleEject,
    PendingEject,
    plate_occupancy,
)

pytestmark = pytest.mark.asyncio


async def _mk_printer(db, name="MPE", gate="SUB-1", model="H2S"):
    p = Printer(
        name=name,
        serial_number=f"S{name}",
        ip_address="1.2.3.4",
        access_code="x",
        model=model,
        plate_gate_subtask_id=gate,
    )
    db.add(p)
    await db.flush()
    return p


async def _mk_item(
    db,
    *,
    printer_id,
    dispatch_subtask="SUB-1",
    first_article=False,
    eject_profile_id=42,
    status="completed",
    started_at=None,
):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        eject_profile_id=eject_profile_id,
        first_article=first_article,
        dispatch_subtask_id=dispatch_subtask,
        started_at=started_at or datetime.now(timezone.utc),
        plate_id=1,
        position=1,
    )
    db.add(item)
    await db.flush()
    return item


def _state(state="FINISH", *, bed=25.0, connected=True):
    return SimpleNamespace(state=state, connected=connected, temperatures={"bed": bed})


def _gate_up(printer_id, *, gate="SUB-1", policy=None):
    """Seed an OCCUPIED plate on ``printer_id`` through the authority.

    ``policy`` is what happens to that plate next; the escalation-only default is the
    "no watch is armed" shape, and a :class:`CooldownEject` is the armed-watch shape
    the manual lane's fast path keys on."""
    plate_occupancy.hydrate_plate(printer_id, gate, policy or EscalationOnly())


def _cooldown(printer_id, unit_id, *, gate="SUB-1", run_id=None):
    """Seed the plate with an armed PRODUCTION cooldown policy for ``unit_id``."""
    _gate_up(printer_id, gate=gate, policy=CooldownEject(unit_id=unit_id, run_id=run_id))


async def _armed_printer(db, name, *, gate="SUB-1", bed_item_kwargs=None):
    """A connected printer whose plate is cooling for a REAL, sweepable farm unit.

    The unit has to exist: the plate's ``CooldownEject`` names a queue row, and a policy
    naming a row that is gone is a state the resolver deliberately falls out of (the
    plate still holds a part, so it becomes an operator confirm rather than a sweep of a
    ghost)."""
    printer = await _mk_printer(db, name, gate=gate)
    item = await _mk_item(db, printer_id=printer.id, dispatch_subtask=gate, **(bed_item_kwargs or {}))
    await db.commit()
    _cooldown(printer.id, item.id, gate=gate)
    return printer, item


def _claim_eject(printer_id, pending):
    """Claim the (already gated) plate for a LIVE eject — the in-flight shape."""
    assert plate_occupancy.claim_for_eject(printer_id, pending, Evidence()) is None


def _connected(status):
    """The two printer_manager patches that pass the connect + live-state gates.

    The plate gate is no longer one of them: it is real occupancy state, seeded with
    :func:`_gate_up`, because the service reads it from the authority."""
    return (
        patch.object(manual.printer_manager, "is_connected", return_value=True),
        patch.object(manual.printer_manager, "get_status", return_value=status),
    )


class TestManualEjectPreconditions:
    async def test_unknown_printer_is_refused_not_found(self, db_session):
        verdict = await manual.manual_eject(db_session, 999999)
        assert verdict.outcome == "refused"
        assert verdict.reason == "not_found"
        assert verdict.mode is None

    async def test_not_connected_refused(self, db_session):
        printer = await _mk_printer(db_session, "NC")
        await db_session.commit()
        with patch.object(manual.printer_manager, "is_connected", return_value=False):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "not_connected"

    async def test_running_printer_refused_job_active(self, db_session):
        """The service's own RUNNING/PAUSE guard is gone: the AUTHORITY answers "is a job
        on this printer?" for both lanes, so the refusal now carries the state machine's
        own token."""
        printer = await _mk_printer(db_session, "BUSY")
        await db_session.commit()
        _gate_up(printer.id)
        c1, c2 = _connected(_state("RUNNING"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "job_active"

    @pytest.mark.parametrize("live_state", ["PREPARE", "SLICING", "PAUSE"])
    async def test_every_active_state_refuses_job_active(self, db_session, live_state):
        # PREPARE/SLICING used to slip past the old two-state guard entirely.
        printer = await _mk_printer(db_session, f"BUSY{live_state}")
        await db_session.commit()
        _gate_up(printer.id)
        c1, c2 = _connected(_state(live_state))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "job_active"

    async def test_a_lost_z_reference_refuses_the_operators_eject_too(self, db_session):
        """2026-09-04 002-H2S: the operator pressed "Eject plate" on a printer that had
        rebooted with a part on the plate, and the sweep drove the bed past the Z floor
        because the block's absolute Z moves ran against a frame the reboot destroyed.

        The manual door is the one an operator reaches for after an outage, so it is the
        one that most needs the refusal to be REACHABLE — the evidence therefore comes
        from the eject lane's one origin, not from a second test in this service."""
        from backend.app.models.printer_incident import KIND_Z_REFERENCE_LOST

        printer = await _mk_printer(db_session, "ZLOST")
        await db_session.commit()
        _gate_up(printer.id)
        c1, c2 = _connected(_state("IDLE"))
        with (
            c1,
            c2,
            patch(
                "backend.app.services.printer_incidents.snapshot",
                return_value={"kind": KIND_Z_REFERENCE_LOST, "status": "escalated"},
            ),
        ):
            verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
        assert verdict.outcome == "refused"
        # The token is spelling-identical to the authority's — one refusal vocabulary
        # from the state machine to the dialog — and the map's AssertionError guard
        # (which fails loud on an unanswered token) is satisfied by it existing here.
        assert verdict.reason == "z_unreferenced"
        assert manual._REFUSAL_REASONS["z_unreferenced"] == "z_unreferenced"

    async def test_gate_down_without_declare_refuses_no_plate_gate(self, db_session):
        """The authority's ``not_occupied`` reaches the API under the name it has always
        had there. It survives ONLY for declare-less callers — every UI surface declares."""
        printer = await _mk_printer(db_session, "NG")
        await db_session.commit()
        c1, c2 = _connected(_state("FINISH"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "no_plate_gate"

    async def test_live_eject_in_flight_refusal_carries_started_and_age(self, db_session):
        """A LIVE eject owns the printer. The refusal ships the two facts the operator
        needs to tell the two situations apart: whether the printer has STARTED the sweep
        (and will therefore finish it on its own) and how long ago it was dispatched."""
        printer = await _mk_printer(db_session, "INF")
        await db_session.commit()
        _gate_up(printer.id)
        _claim_eject(printer.id, PendingEject("production", None, 5))

        c1, c2 = _connected(_state("FINISH"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "refused"
        assert verdict.reason == "eject_in_flight"
        assert verdict.started is False  # dispatched, never echoed a start
        assert isinstance(verdict.age_s, float)
        assert verdict.age_s >= 0.0

    async def test_started_live_eject_reports_started_true(self, db_session):
        printer = await _mk_printer(db_session, "INFSTART")
        await db_session.commit()
        _gate_up(printer.id)
        _claim_eject(printer.id, PendingEject("production", None, 5))
        plate_occupancy.note_eject_started(printer.id)

        c1, c2 = _connected(_state("FINISH"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "eject_in_flight"
        assert verdict.started is True

    async def test_committed_unsettled_lease_refuses_dispatch_in_flight(self, db_session):
        # Nothing to declare (the gate is already up), so the lease is the live conflict:
        # a unit is on its way to this printer and an eject would collide with it.
        printer = await _mk_printer(db_session, "LEASE")
        await db_session.commit()
        # The lease is claimed on a CLEAR plate (that is its precondition); the deposit
        # that raises the gate lands while the unit is still settling on the wire.
        lease = plate_occupancy.claim_for_dispatch(
            printer.id,
            77,
            pre_state="FINISH",
            pre_subtask=None,
            min_hold_s=600.0,
            max_hold_s=900.0,
            ev=Evidence(live_state="FINISH"),
        )
        assert plate_occupancy.commit_dispatch(printer.id, lease) is None
        _gate_up(printer.id)

        c1, c2 = _connected(_state("FINISH"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "dispatch_in_flight"

    async def test_hydrated_eject_supersedes_instead_of_refusing_20260830(self, db_session):
        """THE 2026-08-30 DEAD-END PIN (printer 4, 01:46-01:49, eight consecutive
        ``eject_in_flight`` 409s until the operator hand-jogged the toolhead).

        A HYDRATED eject was rebuilt from a durable timestamp: it has no start echo,
        no runtime estimate and no watchdog, so the farm has already ADMITTED it cannot
        verify that sweep. Refusing an operator in order to protect such a record is
        backwards — the operator's eject is allowed through and supersedes it
        downstream at ``claim_for_eject``."""
        printer = await _mk_printer(db_session, "HYD", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-1")
        plate_occupancy.hydrate_eject(printer.id, PendingEject("production", None, 999))
        assert plate_occupancy.eject_identity(printer.id).hydrated is True

        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id)

        assert verdict.outcome == "dispatched"
        assert verdict.queue_item_id == item.id
        assert verdict.mode == "dispatched"
        dispatch.assert_awaited_once()

    async def test_first_article_is_refused_and_never_swept(self, db_session):
        """BYTE-IDENTICAL BEHAVIOUR: a completed FA unit matching the gate is a hard
        refusal, never weakened into a sweep and never offered as an operator confirm —
        the approval flow owns that plate. Only the code changed (``no_eligible_unit`` →
        the explicit ``first_article``)."""
        printer = await _mk_printer(db_session, "FA", gate="SUB-1")
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", first_article=True)
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-1")
        donor = AsyncMock()
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH"))
        with (
            c1,
            c2,
            patch.object(manual, "resolve_donor", donor),
            patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "refused"
        assert verdict.reason == "first_article"
        donor.assert_not_called()  # no donor work at all for an FA plate
        dispatch.assert_not_called()

    async def test_first_article_policy_alone_refuses(self, db_session):
        # The AUTHORITY's own FA policy is an FA statement, whatever the gate id says.
        printer = await _mk_printer(db_session, "FAPOL", gate=None)
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask=None, first_article=True)
        await db_session.commit()
        _gate_up(printer.id, gate=None, policy=FirstArticleEject(unit_id=item.id, run_id=None))
        c1, c2 = _connected(_state("FINISH"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "first_article"


class TestManualEjectItemResolution:
    """``_resolve_manual_eject_item`` asks the AUTHORITY first — the plate's own policy is
    what the farm decided this deposit is for — then two DB questions that answer
    different things."""

    async def test_cooldown_policy_names_the_unit(self, db_session):
        printer = await _mk_printer(db_session, "RESPOL", gate="SUB-1")
        older = await _mk_item(
            db_session,
            printer_id=printer.id,
            dispatch_subtask="SUB-1",
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        # A NEWER gate-matching unit exists, so the assertion can only pass through the
        # policy — the DB ladder would answer with this one.
        newer = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        _cooldown(printer.id, older.id)

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, "SUB-1")
        assert resolution.lane == "eject"
        assert resolution.item.id == older.id
        assert resolution.item.id != newer.id
        assert resolution.first_article is False

    async def test_a_first_article_under_a_cooldown_policy_still_refuses(self, db_session):
        # A contradictory record (the cooldown policy is never minted for an FA), but the
        # red line is about the PART on the plate, not about how the farm labelled it.
        printer = await _mk_printer(db_session, "RESFAPOL", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", first_article=True)
        await db_session.commit()
        _cooldown(printer.id, item.id)

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, "SUB-1")
        assert resolution.lane == "first_article"
        assert resolution.first_article is True

    async def test_cooldown_policy_naming_a_vanished_row_falls_through(self, db_session):
        # A policy naming a queue row that is gone must not produce a sweep of a ghost;
        # the plate still holds a part, so the ladder (and then the operator) takes over.
        printer = await _mk_printer(db_session, "RESGHOST", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        _cooldown(printer.id, 987654)

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, "SUB-1")
        assert resolution.lane == "eject"
        assert resolution.item.id == item.id

    async def test_falls_back_to_the_gate_matched_db_unit(self, db_session):
        printer = await _mk_printer(db_session, "RESDB", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-1")

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, "SUB-1")
        assert resolution.lane == "eject"
        assert resolution.item.id == item.id

    async def test_gate_key_matching_no_unit_resolves_nothing(self, db_session):
        printer = await _mk_printer(db_session, "RESMIS", gate="SUB-1")
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="OTHER")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-1")

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, "SUB-1")
        assert resolution.lane == "none"
        assert resolution.item is None

    async def test_gate_naming_an_unsweepable_unit_is_the_needs_input_lane(self, db_session):
        # A gate-matched unit that never completed: today's ``:632``-class hard refusal.
        # It is farm-known, so it is never treated as foreign — it becomes an operator
        # confirm built from that unit's own donor.
        printer = await _mk_printer(db_session, "RESINEL", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", status="failed")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-1")

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, "SUB-1")
        assert resolution.lane == "needs_input"
        assert resolution.item.id == item.id

    async def test_a_source_less_gate_resolves_nothing(self, db_session):
        printer = await _mk_printer(db_session, "RESNULL", gate=None)
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        _gate_up(printer.id, gate=None)

        resolution = await manual._resolve_manual_eject_item(db_session, printer.id, None)
        assert resolution.lane == "none"


class TestManualEjectThermal:
    async def test_bed_hot_carries_temps(self, db_session):
        printer, _item = await _armed_printer(db_session, "HOT")
        c1, c2 = _connected(_state("FINISH", bed=50.0))
        with c1, c2, patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)):
            verdict = await manual.manual_eject(db_session, printer.id, allow_hot=False)
        assert verdict.outcome == "bed_hot"
        assert verdict.bed_c == 50.0
        assert verdict.threshold_c == 30.0
        assert verdict.mode is None

    async def test_allow_hot_bypasses_thermal(self, db_session):
        printer, item = await _armed_printer(db_session, "HOTOK")
        c1, c2 = _connected(_state("FINISH", bed=50.0))
        with (
            c1,
            c2,
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=True),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
        ):
            verdict = await manual.manual_eject(db_session, printer.id, allow_hot=True)
        assert verdict.outcome == "released_watch"
        assert verdict.queue_item_id == item.id

    async def test_bed_unreadable_is_a_retryable_refusal_not_bed_hot(self, db_session):
        # Connected but no live bed reading (post-reconnect telemetry window): a
        # bed_unreadable refusal, NEVER bed_hot — the confirm dialog must not be built on
        # a missing reading (the frontend would render Number(null) → "0 °C").
        printer, _item = await _armed_printer(db_session, "NOBED")
        c1, c2 = _connected(_state("FINISH", bed=None))
        with c1, c2, patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)):
            verdict = await manual.manual_eject(db_session, printer.id, allow_hot=False)
        assert verdict.outcome == "refused"
        assert verdict.reason == "bed_unreadable"
        assert verdict.bed_c is None

    async def test_bed_unreadable_allow_hot_proceeds(self, db_session):
        printer, item = await _armed_printer(db_session, "NOBEDOK")
        c1, c2 = _connected(_state("FINISH", bed=None))
        with (
            c1,
            c2,
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=True),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
        ):
            verdict = await manual.manual_eject(db_session, printer.id, allow_hot=True)
        assert verdict.outcome == "released_watch"
        assert verdict.queue_item_id == item.id


class TestManualEjectExecution:
    async def test_armed_cooldown_policy_signals_release_only(self, db_session):
        """The plate's own policy IS the armed watch's identity: a ``CooldownEject``
        for this unit means a watch is running, so the manual eject drives THAT
        watch's single release path instead of racing it with a parallel dispatch."""
        printer, item = await _armed_printer(db_session, "WARM")
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=True) as req,
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "released_watch"
        assert verdict.queue_item_id == item.id
        req.assert_called_once_with(printer.id)
        dispatch.assert_not_called()  # NO parallel dispatch

    async def test_unreleasable_armed_watch_falls_through_to_a_direct_dispatch(self, db_session):
        """``request_release_now`` answers False when the armed record has no release
        channel (nothing armed yet, or an escalation-only hold). The manual lane must
        then dispatch itself rather than silently doing nothing."""
        printer, item = await _armed_printer(db_session, "WCOLD")
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch.object(manual.eject_cooldown_monitor, "request_release_now", return_value=False),
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "dispatched"
        assert verdict.queue_item_id == item.id
        dispatch.assert_awaited_once()

    async def test_no_watch_dispatches(self, db_session):
        printer = await _mk_printer(db_session, "DISP", gate="SUB-1")
        item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-1")
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "dispatched"
        assert verdict.queue_item_id == item.id
        assert dispatch.await_args.kwargs["queue_item_id"] == item.id
        assert dispatch.await_args.kwargs["purpose"] == "production"


_FOREIGN_PLATE_GCODE = (
    "; HEADER_BLOCK_START\n"
    "; max_z_height: 18.00\n"
    "; HEADER_BLOCK_END\n"
    "; EXECUTABLE_BLOCK_START\n"
    "G1 X10 Y10\n"
    "; EXECUTABLE_BLOCK_END\n"
)


def _make_source_3mf() -> Path:
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Metadata/plate_1.gcode", _FOREIGN_PLATE_GCODE)
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


def _make_bare_3mf() -> Path:
    """A .gcode.3mf with NO G-code plate — list_gcode_plate_ids → []."""
    fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
    os.close(fd)
    path = Path(name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


async def _mk_archive(db, *, printer_id, subtask, file_path, filename="foreign.gcode.3mf", print_name="Foreign Widget"):
    arch = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        file_path=file_path,
        file_size=123,
        subtask_id=subtask,
        print_name=print_name,
        status="completed",
    )
    db.add(arch)
    await db.flush()
    return arch


class TestManualEjectForeignPlate:
    """A gate raised by a print the farm did not dispatch: the donor resolves from the
    archive, the operator is asked for a profile, and the confirm sweeps."""

    async def test_foreign_plate_needs_input_payload(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGN", gate="SUB-F")
            # A prior eject-profiled unit (NOT gate-matching → stays foreign) seeds the
            # profile suggestion.
            await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="OTHER", eject_profile_id=77)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.outcome == "needs_input"
            assert verdict.origin == "foreign"
            assert verdict.print_name == "Foreign Widget"
            assert verdict.max_z_height_mm == 18.0
            assert verdict.suggested_eject_profile_id == 77
            assert verdict.mode is None
            assert verdict.reason is None
        finally:
            source.unlink(missing_ok=True)

    async def test_confirm_with_profile_dispatches_foreign(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGC", gate="SUB-F")
            prof = EjectProfile(name="fgc-ep")
            db_session.add(prof)
            await db_session.flush()
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert verdict.outcome == "dispatched"
            assert verdict.queue_item_id is None
            dispatch.assert_awaited_once()
            assert dispatch.await_args.kwargs["printer_id"] == printer.id
            assert dispatch.await_args.kwargs["profile_id"] == prof.id
            assert dispatch.await_args.kwargs["plate_id"] == 1
        finally:
            source.unlink(missing_ok=True)

    async def test_confirm_hot_bed_uses_profile_cooldown(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGH", gate="SUB-F")
            prof = EjectProfile(name="fgh-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            c1, c2 = _connected(_state("FINISH", bed=45.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert verdict.outcome == "bed_hot"
            assert verdict.threshold_c == 30.0
            assert verdict.bed_c == 45.0
        finally:
            source.unlink(missing_ok=True)

    async def test_unknown_profile_on_the_confirm_leg_is_refused(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FGPROF", gate="SUB-F")
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id, eject_profile_id=987654)
            assert verdict.outcome == "refused"
            assert verdict.reason == "profile_not_found"
        finally:
            source.unlink(missing_ok=True)

    async def test_source_less_gate_with_nothing_on_disk_is_no_donor(self, db_session):
        printer = await _mk_printer(db_session, "FGN0", gate=None)
        await db_session.commit()
        _gate_up(printer.id, gate=None)
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "refused"
        assert verdict.reason == "no_donor"

    async def test_no_archive_and_no_fallback_is_no_donor(self, db_session):
        printer = await _mk_printer(db_session, "FGNA", gate="SUB-F")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-F")
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "no_donor"

    async def test_archive_file_missing_and_fetch_fails_is_no_donor(self, db_session):
        # Fallback archive row (file_path="") → FTPS re-fetch attempted; unfetchable, no
        # last-farm-item file and no library slice → every tier declines.
        printer = await _mk_printer(db_session, "FGFF", gate="SUB-F")
        await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path="", filename="gone.gcode.3mf")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-F")
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch(
                "backend.app.services.bambu_ftp.download_file_try_paths_async",
                AsyncMock(return_value=False),
            ),
        ):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "no_donor"


async def _mk_library_file(db, filename):
    lf = LibraryFile(filename=filename, file_path=f"/lib/{filename}", file_type="3mf", file_size=1)
    db.add(lf)
    await db.flush()
    return lf


async def _mk_farm_item(
    db,
    *,
    printer_id,
    library_file_id=None,
    eject_profile_id=None,
    batch_id=None,
    archive_id=None,
    plate_id=None,
    status="cancelled",
):
    """A farm queue item (eject_profile_id OR a farm batch) dispatched to a printer —
    the identity anchor identify_farm_file_foreign matches a foreign echo against, and
    the donor anchor the last-farm-item tier resolves the plate from."""
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,  # the incident shape: farm units were cancelled
        first_article=False,
        eject_profile_id=eject_profile_id,
        library_file_id=library_file_id,
        archive_id=archive_id,
        batch_id=batch_id,
        plate_id=plate_id,
        started_at=datetime.now(timezone.utc),
        position=1,
    )
    db.add(item)
    await db.flush()
    return item


async def _mk_ondisk_library_file(db, *, filename, file_path, sliced_for_model=None):
    """A library row whose ``file_path`` is a REAL on-disk (absolute) donor."""
    lf = LibraryFile(
        filename=filename,
        file_path=file_path,
        file_type="3mf",
        file_size=1,
        file_metadata={"sliced_for_model": sliced_for_model} if sliced_for_model is not None else None,
    )
    db.add(lf)
    await db.flush()
    return lf


class TestManualEjectLastFarmItemFallback:
    """The screen-RESTART incident fix, now the chain's second tier: when the strict
    archive tier declines (blank gate id + a download-failed fallback archive), the
    MANUAL chain falls back to the printer's last-started farm item's on-disk donor. The
    AUTO chain stays fail-closed (pinned in ``test_donor.py``)."""

    @pytest.mark.parametrize("gate", ["", None])
    async def test_blank_gate_falls_back_to_last_item_library_donor(self, db_session, gate):
        source = _make_source_3mf()  # plate_1, max_z 18.0mm
        try:
            printer = await _mk_printer(db_session, "FBLIB", gate=gate)
            prof = EjectProfile(name="fblib-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_ondisk_library_file(db_session, filename="Farm Widget.gcode.3mf", file_path=str(source))
            await _mk_farm_item(
                db_session,
                printer_id=printer.id,
                library_file_id=lf.id,
                eject_profile_id=prof.id,
                plate_id=1,
            )
            await db_session.commit()
            _gate_up(printer.id, gate=gate or None)
            # First call (no profile) → the confirm prompt, carrying the item's donor.
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.outcome == "needs_input"
            assert verdict.origin == "foreign"  # not declared: the operator stated nothing
            assert verdict.print_name == "Farm Widget.gcode.3mf"
            assert verdict.max_z_height_mm == 18.0
            # Second call (profile chosen) → dispatch the sweep with the fallback donor.
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert verdict.outcome == "dispatched"
            assert verdict.queue_item_id is None
            dispatch.assert_awaited_once()
            assert dispatch.await_args.kwargs["printer_id"] == printer.id
            assert dispatch.await_args.kwargs["profile_id"] == prof.id
            assert dispatch.await_args.kwargs["plate_id"] == 1  # the item's own plate
            assert dispatch.await_args.kwargs["source_path"] == source  # the on-disk fallback donor
        finally:
            source.unlink(missing_ok=True)

    async def test_archive_donor_preferred_over_library(self, db_session):
        source_arch = _make_source_3mf()
        source_lib = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FBARCH", gate="")
            prof = EjectProfile(name="fbarch-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            # Archive donor (subtask "OTHER" → the strict gate tier never finds it).
            archive = await _mk_archive(
                db_session,
                printer_id=printer.id,
                subtask="OTHER",
                file_path=str(source_arch),
                filename="arch.gcode.3mf",
                print_name="Arch Widget",
            )
            lf = await _mk_ondisk_library_file(db_session, filename="Lib Widget.gcode.3mf", file_path=str(source_lib))
            await _mk_farm_item(
                db_session,
                printer_id=printer.id,
                archive_id=archive.id,
                library_file_id=lf.id,
                eject_profile_id=prof.id,
                plate_id=1,
            )
            await db_session.commit()
            _gate_up(printer.id, gate=None)
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.print_name == "Arch Widget"
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert dispatch.await_args.kwargs["source_path"] == source_arch
        finally:
            source_arch.unlink(missing_ok=True)
            source_lib.unlink(missing_ok=True)

    async def test_no_last_item_and_no_library_is_no_donor(self, db_session):
        printer = await _mk_printer(db_session, "FBNONE", gate="")
        await db_session.commit()
        _gate_up(printer.id, gate=None)
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "refused"
        assert verdict.reason == "no_donor"

    async def test_donor_missing_on_disk_is_no_donor(self, db_session):
        # The last item's library donor is not on disk, and the row is not a model slice
        # either → no tier answers.
        printer = await _mk_printer(db_session, "FBMISS", gate="")
        lf = await _mk_ondisk_library_file(
            db_session, filename="Gone.gcode.3mf", file_path="/nonexistent/Gone.gcode.3mf"
        )
        await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, plate_id=1)
        await db_session.commit()
        _gate_up(printer.id, gate=None)
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "no_donor"

    async def test_plateless_donor_is_no_donor(self, db_session):
        # The donor exists but carries no G-code plate → nothing to repack, at any tier.
        bare = _make_bare_3mf()
        try:
            printer = await _mk_printer(db_session, "FBBARE", gate="")
            lf = await _mk_ondisk_library_file(
                db_session, filename="Bare.gcode.3mf", file_path=str(bare), sliced_for_model="H2S"
            )
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, plate_id=1)
            await db_session.commit()
            _gate_up(printer.id, gate=None)
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.reason == "no_donor"
        finally:
            bare.unlink(missing_ok=True)


class TestManualEjectFarmUnitConfirm:
    """A plate the farm KNOWS but this lane cannot sweep from the unit's own record. It
    used to be a hard 409 dead end; it is now the same operator confirm, labelled so the
    dialog can name the unit."""

    async def test_farm_unit_without_an_eject_profile_asks_the_operator(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FUNOP", gate="SUB-1")
            item = await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", eject_profile_id=None)
            await _mk_archive(
                db_session,
                printer_id=printer.id,
                subtask="SUB-1",
                file_path=str(source),
                print_name="Farm Unit Print",
            )
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-1")
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.outcome == "needs_input"
            assert verdict.origin == "farm_unit"
            assert verdict.print_name == "Farm Unit Print"
            assert verdict.max_z_height_mm == 18.0
            assert item.eject_profile_id is None
        finally:
            source.unlink(missing_ok=True)

    async def test_gate_naming_an_unfinished_unit_asks_the_operator(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FUNFIN", gate="SUB-1")
            await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", status="failed")
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-1", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-1")
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.outcome == "needs_input"
            assert verdict.origin == "farm_unit"
        finally:
            source.unlink(missing_ok=True)

    async def test_farm_unit_confirm_dispatches_the_sweep(self, db_session):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "FUDISP", gate="SUB-1")
            prof = EjectProfile(name="fudisp-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="SUB-1", eject_profile_id=None)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-1", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-1")
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(
                    db_session, printer.id, eject_profile_id=prof.id, max_z_override=21.0
                )
            assert verdict.outcome == "dispatched"
            assert dispatch.await_args.kwargs["max_z_override"] == 21.0
        finally:
            source.unlink(missing_ok=True)


class TestManualEjectContainerDonor:
    """The container tier: an anonymous library slice supplies the ZIP skeleton and
    NOTHING else, so the operator's part height is not optional."""

    async def test_container_prompt_carries_no_height_and_no_name(self, db_session):
        container = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "CNTP", gate=None)
            await _mk_ondisk_library_file(
                db_session, filename="Container.gcode.3mf", file_path=str(container), sliced_for_model="H2S"
            )
            await db_session.commit()
            _gate_up(printer.id, gate=None)
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id)
            assert verdict.outcome == "needs_input"
            assert verdict.max_z_height_mm is None
            assert verdict.print_name is None
        finally:
            container.unlink(missing_ok=True)

    async def test_container_confirm_without_a_height_asks_again(self, db_session):
        """Not an error — a re-prompt. The sweep's clearance and lift are computed from a
        part height, and a container donor has none to give."""
        container = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "CNTNOH", gate=None)
            prof = EjectProfile(name="cntnoh-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_ondisk_library_file(
                db_session, filename="Container.gcode.3mf", file_path=str(container), sliced_for_model="H2S"
            )
            await db_session.commit()
            _gate_up(printer.id, gate=None)
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
            assert verdict.outcome == "needs_input"
            assert verdict.max_z_height_mm is None
            assert verdict.suggested_eject_profile_id == prof.id  # the operator's pick is kept
            dispatch.assert_not_called()  # nothing is ever built without a height
        finally:
            container.unlink(missing_ok=True)

    async def test_container_confirm_with_a_height_dispatches(self, db_session):
        container = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "CNTOK", gate=None)
            prof = EjectProfile(name="cntok-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_ondisk_library_file(
                db_session, filename="Container.gcode.3mf", file_path=str(container), sliced_for_model="H2S"
            )
            await db_session.commit()
            _gate_up(printer.id, gate=None)
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(
                    db_session, printer.id, eject_profile_id=prof.id, max_z_override=30.0
                )
            assert verdict.outcome == "dispatched"
            assert dispatch.await_args.kwargs["source_path"] == container
            assert dispatch.await_args.kwargs["max_z_override"] == 30.0
        finally:
            container.unlink(missing_ok=True)


class TestManualEjectDeclareOccupied:
    """The on-demand lane: with the plate CLEAR, ``declare_occupied`` is the operator
    STATING that a part is on it. The declaration runs BEFORE the eject's own occupancy
    gate (2026-08-30 review F8), so the operator's cure is reachable while a unit is
    mid-upload; the connected guard precedes both, and the raise is NEVER rolled back."""

    async def test_flag_false_keeps_the_no_plate_gate_refusal(self, db_session):
        printer = await _mk_printer(db_session, "DCOFF", gate=None)
        await db_session.commit()
        c1, c2 = _connected(_state("FINISH"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.reason == "no_plate_gate"
        assert plate_occupancy.is_plate_occupied(printer.id) is False

    async def test_running_printer_refuses_and_declares_nothing(self, db_session):
        # Lane partition: a declaration can never gate a printer that is mid-print — its
        # plate is not free to state anything about.
        printer = await _mk_printer(db_session, "DCRUN", gate=None)
        await db_session.commit()
        c1, c2 = _connected(_state("RUNNING"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
        assert verdict.reason == "job_active"
        assert plate_occupancy.is_plate_occupied(printer.id) is False

    async def test_preparing_printer_is_refused_by_the_authority(self, db_session):
        printer = await _mk_printer(db_session, "DCPREP", gate=None)
        await db_session.commit()
        c1, c2 = _connected(_state("PREPARE"))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
        assert verdict.reason == "job_active"
        assert plate_occupancy.is_plate_occupied(printer.id) is False

    async def test_disconnected_printer_refuses_before_the_declaration(self, db_session):
        # The other half of the partition: a disconnected printer's plate is declared
        # through the standalone mark-plate-occupied route, never through an eject.
        printer = await _mk_printer(db_session, "DCNC", gate=None)
        await db_session.commit()
        with patch.object(manual.printer_manager, "is_connected", return_value=False):
            verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
        assert verdict.reason == "not_connected"
        assert plate_occupancy.is_plate_occupied(printer.id) is False

    async def test_gate_raised_once_and_kept_when_no_donor_resolves(self, db_session):
        # The no-rollback pin: the refusal stands AND so does the gate the declaration
        # raised — the plate is occupied whatever the eject concludes.
        printer = await _mk_printer(db_session, "DCNOD", gate=None)
        await db_session.commit()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2:
            verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
        assert verdict.reason == "no_donor"
        view = plate_occupancy.snapshot(printer.id)
        assert view.plate_occupied is True
        assert view.plate_source_subtask_id is None  # source-less ⇒ human-clear-only
        assert isinstance(view.plate_policy, EscalationOnly)

    async def test_declaration_lands_and_revokes_a_lease_instead_of_being_blocked_by_it(self, db_session):
        """THE F8 ORDER PIN + the 2026-08-30 01:06:57 shape.

        The operator's declaration landed between the scheduler's claim and its
        ``start_print``. Declaring FIRST means (a) the lease is REVOKED, so the dispatch
        unwinds instead of printing onto the declared plate, and (b) the eject is not then
        turned away by the very lease it just cancelled — the flow runs all the way to the
        operator's confirm prompt."""
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "DCLEASE", gate=None)
            lf = await _mk_ondisk_library_file(db_session, filename="Farm Widget.gcode.3mf", file_path=str(source))
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, plate_id=1)
            await db_session.commit()
            lease = plate_occupancy.claim_for_dispatch(
                printer.id,
                77,
                pre_state="FINISH",
                pre_subtask=None,
                min_hold_s=60.0,
                max_hold_s=180.0,
                ev=Evidence(live_state="FINISH"),
            )
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)

            assert verdict.outcome == "needs_input"  # NOT refused dispatch_in_flight
            assert verdict.origin == "declared"
            assert plate_occupancy.is_plate_occupied(printer.id) is True
            assert plate_occupancy.commit_dispatch(printer.id, lease) == "lease_revoked"
        finally:
            source.unlink(missing_ok=True)

    async def test_gate_raised_then_fallback_donor_drives_the_confirm_prompt(self, db_session):
        # The whole point of the lane: a plate the farm never gated still reaches the
        # confirm prompt, carrying the last farm item's on-disk donor.
        source = _make_source_3mf()  # plate_1, max_z 18.0mm
        try:
            printer = await _mk_printer(db_session, "DCDON", gate=None)
            prof = EjectProfile(name="dcdon-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_ondisk_library_file(db_session, filename="Farm Widget.gcode.3mf", file_path=str(source))
            await _mk_farm_item(
                db_session,
                printer_id=printer.id,
                library_file_id=lf.id,
                eject_profile_id=prof.id,
                plate_id=1,
            )
            await db_session.commit()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
            assert verdict.outcome == "needs_input"
            assert verdict.origin == "declared"
            assert verdict.print_name == "Farm Widget.gcode.3mf"
            assert verdict.max_z_height_mm == 18.0
            assert verdict.suggested_eject_profile_id == prof.id
            assert plate_occupancy.is_plate_occupied(printer.id) is True
            assert plate_occupancy.plate_source(printer.id) is None
        finally:
            source.unlink(missing_ok=True)

    async def test_confirm_leg_with_the_gate_up_never_double_raises(self, db_session):
        # The dialog's confirm sends the flag again; by then the first call's raise is
        # up, so the branch is skipped entirely and the sweep dispatches.
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "DCCFM", gate="SUB-F")
            prof = EjectProfile(name="dccfm-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(
                    db_session, printer.id, eject_profile_id=prof.id, declare_occupied=True
                )
            assert verdict.outcome == "dispatched"
            dispatch.assert_awaited_once()
            # The standing gate is untouched — a re-declaration would have wiped the
            # source id the confirm is sweeping against.
            assert plate_occupancy.plate_source(printer.id) == "SUB-F"
        finally:
            source.unlink(missing_ok=True)

    async def test_a_stale_row_gate_key_steers_nothing(self, db_session):
        """The failed-persist shape: the authority's plate is CLEAR while the printer ROW
        still carries a key. The donor lane reads ``plate_occupancy.plate_source`` and
        never the column, so the dead key cannot resolve a farm unit or a donor — and
        nothing in this lane writes the column any more either (the old sync-NULL patch is
        deleted along with the staleness it patched)."""
        printer = await _mk_printer(db_session, "DCSTALE", gate="STALE-1")
        await _mk_item(db_session, printer_id=printer.id, dispatch_subtask="STALE-1")
        await db_session.commit()
        farm_known_dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch.object(manual, "_resolve_eject_threshold", AsyncMock(return_value=30.0)),
            patch.object(manual.eject_remote, "dispatch_part_present_eject", farm_known_dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id, declare_occupied=True)
        assert verdict.reason == "no_donor"
        farm_known_dispatch.assert_not_called()
        assert printer.plate_gate_subtask_id == "STALE-1"  # write-only persistence, untouched
        assert plate_occupancy.is_plate_occupied(printer.id) is True  # the raise stands


class TestManualEjectHeightOverride:
    """The operator's corrected part height rides the CONFIRM leg only and reaches the
    build unchanged — the profile's own guard stays the validation authority."""

    async def test_confirm_leg_forwards_the_override_to_the_dispatch(self, db_session):
        source = _make_source_3mf()  # the donor header says 18.0mm
        try:
            printer = await _mk_printer(db_session, "HOVR", gate="SUB-F")
            prof = EjectProfile(name="hovr-ep", cooldown_temp_c=30.0)
            db_session.add(prof)
            await db_session.flush()
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            dispatch = AsyncMock()
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2, patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch):
                verdict = await manual.manual_eject(
                    db_session, printer.id, eject_profile_id=prof.id, max_z_override=42.5
                )
            assert verdict.outcome == "dispatched"
            assert dispatch.await_args.kwargs["max_z_override"] == 42.5
        finally:
            source.unlink(missing_ok=True)

    async def test_prompt_leg_still_carries_the_parsed_donor_height(self, db_session):
        # The first (no-profile) leg is unchanged: the prompt's height is the dialog's
        # PREFILL, so an override arriving there is inert — nothing is built yet.
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "HOVRP", gate="SUB-F")
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            c1, c2 = _connected(_state("FINISH", bed=25.0))
            with c1, c2:
                verdict = await manual.manual_eject(db_session, printer.id, max_z_override=99.0)
            assert verdict.max_z_height_mm == 18.0
        finally:
            source.unlink(missing_ok=True)


class TestCanonicalNames:
    """The identity key that makes a screen-started print's UNDERSCORED, ``.gcode``-less
    USB echo compare equal to the farm's SPACED, token-bearing library/archive name.
    One key only — shared with farm_correlation, which carried a blinder variant until
    2026-08-22."""

    async def test_underscored_echo_matches_spaced_stored_name(self):
        # (async only to satisfy the module-level asyncio pytestmark; the fn is pure.)
        echoed = manual._canonical_names(".6_nozzle_(Battery_holders_X2)", None)
        stored = manual._canonical_names(".6 nozzle (Battery holders X2).gcode.3mf")
        assert echoed == stored
        assert echoed == {".6_nozzle_(battery_holders_x2)"}

    async def test_spliced_corpus_echo_matches_stored_name(self):
        # The 2026-08-22 production defect: the splicer writes a MID-STEM ``.gcode``
        # token and the firmware drops it from the echo. Real pair from the logs.
        echoed = manual._canonical_names("Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced", None)
        stored = manual._canonical_names("Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf")
        assert echoed == stored
        assert echoed == {"rotary_tool_top_surfaces_pco-m12-2525_l1-90_spliced"}

    async def test_emits_exactly_one_key_per_name(self):
        # Not two forms: every call site is an identity comparison, so a stricter
        # member alongside the relaxed one could never decide anything extra.
        assert len(manual._canonical_names("Widget A.gcode_L1-5_spliced.3mf")) == 1

    async def test_blanks_skipped_and_basename_stripped(self):
        assert manual._canonical_names(None, "") == set()
        # A path-prefixed name is basename-stripped before keying.
        assert manual._canonical_names("/data/Widget A.3mf") == {"widget_a"}


class TestIdentifyFarmFileForeign:
    """A foreign completion is auto-ejected ONLY when positively the farm's OWN file —
    name match (canonicalised) AND validated geometry AND a suggested profile AND a
    STRICT-tier donor whose height is within that profile's guard. Any miss → None."""

    async def test_positive_identification_returns_profile_and_threshold(self, db_session, seed_geometry):
        source = _make_source_3mf()  # plate max_z 18.0mm, within the 42mm guard
        try:
            printer = await _mk_printer(db_session, "IDN", gate="SUB-F")  # H2S → validated
            prof = EjectProfile(name="idn-ep", cooldown_temp_c=30.0, max_part_height_mm=42.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")  # SPACED display name
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            # Echoed name = the UNDERSCORED USB filename a screen-start reports.
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename="Farm_Widget.gcode.3mf"
            )
            assert result is not None
            assert result.profile_id == prof.id
            assert result.threshold_c == 30.0
            assert result.print_name == "Foreign Widget"
        finally:
            source.unlink(missing_ok=True)

    async def test_spliced_production_name_is_identified(self, db_session, seed_geometry):
        """THE 2026-08-22 REGRESSION GUARD. Gate (a) refused this farm's entire corpus:
        the splicer writes a MID-STEM ``.gcode`` token that the firmware drops from its
        echo, so the rescue never fired once across 19 FOREIGN terminals in 5 days.
        Real name pair, straight from the production logs."""
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDSPL", gate="SUB-F")  # H2S -> validated
            prof = EjectProfile(name="idspl-ep", cooldown_temp_c=30.0, max_part_height_mm=42.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            result = await manual.identify_farm_file_foreign(
                db_session,
                printer.id,
                subtask_name="Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced",
                filename=None,
            )
            assert result is not None
            assert result.profile_id == prof.id
            assert result.threshold_c == 30.0
        finally:
            source.unlink(missing_ok=True)

    async def test_spliced_name_near_miss_is_not_identified(self, db_session, seed_geometry):
        """The widened key is still not fuzzy: a DIFFERENT layer range is a different
        print and must not be swept as the farm's own plate."""
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDSPLN", gate="SUB-F")
            prof = EjectProfile(name="idspln-ep", cooldown_temp_c=30.0, max_part_height_mm=42.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            result = await manual.identify_farm_file_foreign(
                db_session,
                printer.id,
                subtask_name="Rotary_tool_top_surfaces_PCO-M12-2525_L1-91_spliced",
                filename=None,
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_name_is_not_a_farm_file(self, db_session, seed_geometry):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDNEG", gate="SUB-F")
            prof = EjectProfile(name="idneg-ep")
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Some Other File.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Totally_Unrelated_Local", filename="local.gcode"
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_geometry_unvalidated(self, db_session, seed_geometry):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDU", gate="SUB-F", model="H2C")  # H2C → unvalidated
            prof = EjectProfile(name="idu-ep")
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            # Name matches, but H2C geometry is not hardware-validated → no auto-eject.
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_no_suggested_profile(self, db_session, seed_geometry):
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDNP", gate="SUB-F")
            batch = PrintBatch(name="run", sku_file_id=1)  # farm via batch, NOT an eject-profiled unit
            db_session.add(batch)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, batch_id=batch.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            # Name matches + geometry validated, but no eject-profiled unit → no profile.
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_height_exceeds_profile_guard(self, db_session, seed_geometry):
        source = _make_source_3mf()  # 18.0mm part
        try:
            printer = await _mk_printer(db_session, "IDH", gate="SUB-F")
            prof = EjectProfile(name="idh-ep", max_part_height_mm=10.0)  # guard below the 18mm part
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)

    async def test_negative_when_only_a_weak_tier_could_answer(self, db_session, seed_geometry):
        """THE FAIL-CLOSED PIN. The identify gate walks the AUTO chain, so an assumed
        last-farm-item donor — which the MANUAL chain would happily hand an operator —
        can never justify an unattended sweep."""
        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDWEAK", gate=None)
            prof = EjectProfile(name="idweak-ep", cooldown_temp_c=30.0, max_part_height_mm=42.0)
            db_session.add(prof)
            await db_session.flush()
            lf = await _mk_ondisk_library_file(db_session, filename="Farm Widget.gcode.3mf", file_path=str(source))
            await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
            await db_session.commit()
            _gate_up(printer.id, gate=None)  # source-less gate → the strict tier declines
            result = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename=None
            )
            assert result is None
        finally:
            source.unlink(missing_ok=True)


def _donor_bytes() -> bytes:
    """Valid donor 3MF bytes (plate_1 + max_z 18mm) for a re-fetch mock."""
    src = _make_source_3mf()
    try:
        return src.read_bytes()
    finally:
        src.unlink(missing_ok=True)


class _FetchCounter:
    """A stand-in for ``download_file_try_paths_async`` that WRITES a valid donor to the
    requested temp path (so the fetched file is real) and counts each fetch — the
    Phase D1 dedupe assertion (a cached deposit means the second resolve fetches 0)."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    async def __call__(self, ip, code, remote_paths, dest, printer_model=None):
        self.calls += 1
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(self.payload)
        return True


@pytest.fixture(autouse=True)
def _clear_donor_cache():
    """Isolate the module-level Phase D1 donor cache between tests (unlink temp files)."""
    donor_mod._donor_cache.clear()
    yield
    for _key, (path, _exp) in list(donor_mod._donor_cache.items()):
        path.unlink(missing_ok=True)
    donor_mod._donor_cache.clear()


class TestForeignDonorCache:
    """Phase D1: the operator flow re-fetches the donor only ONCE — the prompt DEPOSITS
    the fetched temp, the confirm CONSUMES it. The auto path (identify → dispatch)
    dedupes the same way. The key is the AUTHORITY's gate, so a re-raise for a DIFFERENT
    subtask never serves the stale donor, and an expired entry is unlinked + re-fetched."""

    async def _fetching_printer(self, db, name, gate="SUB-F", filename="gone.gcode.3mf"):
        """A printer + a download-failed fallback archive (file_path="") so the donor
        resolves via the FTPS re-fetch path (the branch the cache covers), with its
        plate occupied so the manual lane gets past the gate check."""
        printer = await _mk_printer(db, name, gate=gate)
        await _mk_archive(db, printer_id=printer.id, subtask=gate, file_path="", filename=filename)
        _gate_up(printer.id, gate=gate)
        return printer

    async def test_prompt_deposits_and_confirm_consumes_no_second_fetch(self, db_session):
        printer = await self._fetching_printer(db_session, "D1A")
        prof = EjectProfile(name="d1a-ep", cooldown_temp_c=30.0)
        db_session.add(prof)
        await db_session.flush()
        await db_session.commit()
        fetch = _FetchCounter(_donor_bytes())
        # First call (no profile) → the confirm prompt; the donor is fetched + deposited.
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2, patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch):
            verdict = await manual.manual_eject(db_session, printer.id)
        assert verdict.outcome == "needs_input"
        assert fetch.calls == 1
        assert len(donor_mod._donor_cache) == 1  # deposited
        # Second call (profile chosen) → CONSUMES the deposit; NO second fetch.
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch),
            patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
        ):
            verdict = await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
        assert verdict.outcome == "dispatched"
        assert fetch.calls == 1  # consumed the cache — no re-download
        assert len(donor_mod._donor_cache) == 0  # consumed entry removed
        dispatch.assert_awaited_once()

    async def test_expired_entry_is_unlinked_and_refetched(self, db_session):
        printer = await self._fetching_printer(db_session, "D1B")
        prof = EjectProfile(name="d1b-ep", cooldown_temp_c=30.0)
        db_session.add(prof)
        await db_session.flush()
        await db_session.commit()
        fetch = _FetchCounter(_donor_bytes())
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2, patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch):
            await manual.manual_eject(db_session, printer.id)
        assert fetch.calls == 1
        # Force the deposited entry to look expired, then confirm → sweep unlinks it +
        # re-fetch (a fresh download).
        ((key, (deposited_path, _exp)),) = list(donor_mod._donor_cache.items())
        donor_mod._donor_cache[key] = (deposited_path, -1.0)
        assert deposited_path.is_file()
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch),
            patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
        ):
            await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
        assert fetch.calls == 2  # expired → re-fetched
        assert not deposited_path.is_file()  # the expired temp was unlinked by the sweep

    async def test_different_gate_does_not_consume_stale_donor(self, db_session):
        printer = await self._fetching_printer(db_session, "D1C", gate="SUB-A", filename="a.gcode.3mf")
        prof = EjectProfile(name="d1c-ep", cooldown_temp_c=30.0)
        db_session.add(prof)
        await db_session.flush()
        await db_session.commit()
        fetch = _FetchCounter(_donor_bytes())
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with c1, c2, patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch):
            await manual.manual_eject(db_session, printer.id)
        assert fetch.calls == 1
        # A NEW print raises a DIFFERENT gate subtask on the same printer — through the
        # AUTHORITY, which is where the cache key now comes from.
        await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-B", file_path="", filename="b.gcode.3mf")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-B")
        dispatch = AsyncMock()
        c1, c2 = _connected(_state("FINISH", bed=25.0))
        with (
            c1,
            c2,
            patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch),
            patch.object(manual.eject_remote, "dispatch_foreign_eject", dispatch),
        ):
            await manual.manual_eject(db_session, printer.id, eject_profile_id=prof.id)
        # The gate-B confirm did NOT serve the gate-A donor → a fresh fetch.
        assert fetch.calls == 2

    async def test_auto_path_identify_then_dispatch_fetches_once(self, db_session, seed_geometry, monkeypatch):
        import contextlib

        printer = await _mk_printer(db_session, "D1D", gate="SUB-F")  # H2S → validated
        prof = EjectProfile(name="d1d-ep", cooldown_temp_c=30.0, max_part_height_mm=42.0)
        db_session.add(prof)
        await db_session.flush()
        lf = await _mk_library_file(db_session, "Farm Widget.gcode.3mf")
        await _mk_farm_item(db_session, printer_id=printer.id, library_file_id=lf.id, eject_profile_id=prof.id)
        # Download-failed fallback archive → the auto path also resolves via re-fetch.
        await _mk_archive(
            db_session, printer_id=printer.id, subtask="SUB-F", file_path="", filename="Farm_Widget.gcode.3mf"
        )
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-F")
        fetch = _FetchCounter(_donor_bytes())

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)

        with patch("backend.app.services.bambu_ftp.download_file_try_paths_async", fetch):
            identified = await manual.identify_farm_file_foreign(
                db_session, printer.id, subtask_name="Farm_Widget", filename="Farm_Widget.gcode.3mf"
            )
            assert identified is not None
            assert fetch.calls == 1  # identify fetched + deposited
            dispatch = AsyncMock()
            with patch.object(eject_remote, "dispatch_foreign_eject", dispatch):
                await eject_remote.dispatch_identified_foreign_eject(
                    printer_id=printer.id, profile_id=identified.profile_id
                )
        assert fetch.calls == 1  # dispatch consumed the deposit — no second fetch
        dispatch.assert_awaited_once()


class TestDispatchIdentifiedForeignEject:
    """The auto foreign-eject on_release: resolve the donor through the AUTO chain and
    dispatch the sweep (no thermal gate — the cooldown watch already waited).

    The entry point lives in ``eject.remote``, beside the dispatcher it calls, so the
    monitor no longer reaches into the manual-eject service through a lazy import."""

    async def test_dispatches_foreign_eject_for_gate_source(self, db_session, monkeypatch):
        import contextlib

        source = _make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "IDD", gate="SUB-F")
            await _mk_archive(db_session, printer_id=printer.id, subtask="SUB-F", file_path=str(source))
            await db_session.commit()
            _gate_up(printer.id, gate="SUB-F")

            @contextlib.asynccontextmanager
            async def _fake_session():
                yield db_session

            # dispatch_identified_foreign_eject opens its OWN session — back it with db_session.
            monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)
            dispatch = AsyncMock()
            with patch.object(eject_remote, "dispatch_foreign_eject", dispatch):
                await eject_remote.dispatch_identified_foreign_eject(printer_id=printer.id, profile_id=5)
            dispatch.assert_awaited_once()
            assert dispatch.await_args.kwargs["printer_id"] == printer.id
            assert dispatch.await_args.kwargs["profile_id"] == 5
            assert dispatch.await_args.kwargs["plate_id"] == 1
        finally:
            source.unlink(missing_ok=True)

    async def test_unresolvable_donor_raises_so_the_watch_counts_a_failure(self, db_session, monkeypatch):
        """It must RAISE, never return quietly: ``watch_bed_and_clear`` counts a dispatch
        failure (retry, then stall after three), and a silent drop would leave the plate
        gated with nothing watching it."""
        import contextlib

        printer = await _mk_printer(db_session, "IDDNO", gate="SUB-F")
        await db_session.commit()
        _gate_up(printer.id, gate="SUB-F")

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)
        with pytest.raises(eject_remote.EjectDispatchError) as exc:
            await eject_remote.dispatch_identified_foreign_eject(printer_id=printer.id, profile_id=5)
        assert exc.value.code == "no_donor"


class TestDeletedSymbols:
    """Absence pins for what the cut-over and this rewrite deleted.

    ``active_watch_identity`` / ``_ActiveWatch`` were the monitor's second opinion about
    which unit a plate was cooling for — one of the five stores the 2026-08-30 cut-over
    collapsed; the plate's own ``CooldownEject`` policy is now that fact. The three
    exception classes were the manual lane's control flow, replaced by one verdict: a
    re-appearance means an eject outcome is once again being signalled by unwinding the
    stack past the code that has to decide what it MEANS."""

    async def test_monitor_no_longer_exposes_a_watch_identity_registry(self):
        from backend.app.services.eject import monitor as monitor_mod

        assert not hasattr(monitor_mod, "_ActiveWatch"), "eject.monitor._ActiveWatch must stay deleted"
        for name in (
            "active_watch_identity",
            "_watching",
            "start_escalation_only_watch",
            "start_fa_eject_watch",
            "start_foreign_eject_watch",
            "on_terminal_status",
            "rearm_on_startup",
        ):
            assert not hasattr(monitor_mod.eject_cooldown_monitor, name), (
                f"EjectCooldownMonitor.{name} must stay deleted"
            )

    async def test_the_manual_eject_exception_classes_stay_deleted(self):
        for name in ("ManualEjectError", "ForeignPlateEject", "BedTooHot"):
            assert not hasattr(manual, name), f"eject.manual.{name} must stay deleted — the verdict replaced it"
