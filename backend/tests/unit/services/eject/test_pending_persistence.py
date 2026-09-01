"""The durable pending-eject mirror (``print_queue.eject_dispatched_at``).

The mirror itself is unchanged — it is still the ONE durable witness that an eject was
dispatched, still printer-scoped on release, and still what a restart rebuilds a pending
from. What changed on 2026-08-30 is WHO writes it: ``eject.remote`` holds no eject state
any more, so the stamp is no longer written by the dispatcher. It is written by
``plate_occupancy_store.persist_occupancy`` — the persist callable the authority fans
every completed transition out through — and rebuilt by ``plate_occupancy_store.hydrate``.

These tests therefore drive real TRANSITIONS (``claim_for_eject`` / ``resolve_eject``,
and the full dispatcher) with the persist callable wired, and assert on the DB. That is
the seam they own: ``test_plate_occupancy_store.py`` covers ``persist_occupancy`` and
``hydrate`` called DIRECTLY (including the 24 h TTL, one-pending-per-printer and the
NULLed losers), while what is pinned here is that the eject lane's own transitions
actually reach that callable — the wiring between the record and its durable half.
"""

import asyncio
import contextlib
import os
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.services import plate_occupancy_store as store
from backend.app.services.eject import remote
from backend.app.services.plate_occupancy import (
    EscalationOnly,
    Evidence,
    PendingEject,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset_occupancy():
    """Every test starts on an empty fleet and leaves no eject timer armed."""
    plate_occupancy.reset_for_tests()
    yield
    for printer_id in list(remote._start_deadlines) + list(remote._runtime_watchdogs):
        remote.cancel_eject_timers(printer_id)
    plate_occupancy.reset_for_tests()


@pytest.fixture
def scheduled(monkeypatch):
    """Capture what the persist callable schedules, so a test can await its write."""
    tasks: list[asyncio.Task] = []

    def _spawn(coro, *, name=None):
        task = asyncio.ensure_future(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(store, "spawn_background_task", _spawn)
    return tasks


async def _drain(tasks: list[asyncio.Task]) -> None:
    if tasks:
        await asyncio.gather(*tasks)


def _patch_session(monkeypatch, db_session):
    @contextlib.asynccontextmanager
    async def _fake_session():
        yield db_session

    monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)


def _wire_persist():
    """Inject ONLY the durable mirror.

    ``configure`` leaves an omitted callable untouched, so the broadcast / kick / policy
    lanes stay at their no-op defaults and a transition here writes the DB and nothing
    else — which is exactly the half these tests are about."""
    plate_occupancy.configure(persist=store.persist_occupancy)


def _gate(printer_id: int, *, source: str | None = None) -> None:
    """Raise the plate gate (an eject is refused ``not_occupied`` on a clear plate)."""
    plate_occupancy.hydrate_plate(printer_id, source, EscalationOnly())


async def _mk_item(db, *, printer_id, first_article=False, batch_id=None, status="completed", stamp=None):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=first_article,
        batch_id=batch_id,
        plate_id=1,
        position=1,
        eject_dispatched_at=stamp,
    )
    db.add(item)
    await db.flush()
    return item


async def _mk_printer(db, name="PP"):
    p = Printer(name=name, serial_number=f"S{name}", ip_address="1.2.3.4", access_code="x", model="H2S")
    db.add(p)
    await db.flush()
    return p


class TestDurableMirrorFollowsTheTransition:
    """A claim stamps, a retirement NULLs — through the authority's fan-out, so the
    in-memory record and its durable half cannot disagree."""

    async def test_a_live_production_claim_stamps_the_owning_unit(self, db_session, monkeypatch, scheduled):
        printer = await _mk_printer(db_session, "PST")
        item = await _mk_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        _wire_persist()
        _gate(printer.id)

        assert (
            plate_occupancy.claim_for_eject(
                printer.id,
                PendingEject("production", None, item.id, expected_runtime_s=83.0),
                Evidence(),
            )
            is None
        )
        await _drain(scheduled)

        await db_session.refresh(item)
        assert item.eject_dispatched_at is not None
        # The mirror carries the claim's OWN dispatch time, not the write's.
        identity = plate_occupancy.eject_identity(printer.id)
        assert store._as_utc(item.eject_dispatched_at) == store._as_utc(identity.dispatched_at)

    async def test_a_claim_naming_a_missing_unit_writes_nothing_and_never_raises(
        self, db_session, monkeypatch, scheduled
    ):
        # A queue_item_id that does not exist must not take the transition down with it:
        # an occupancy fact that already happened may never be unwound by a DB miss.
        _patch_session(monkeypatch, db_session)
        _wire_persist()
        _gate(12345)

        assert plate_occupancy.claim_for_eject(12345, PendingEject("production", None, 999999), Evidence()) is None
        await _drain(scheduled)

        assert plate_occupancy.eject_identity(12345) is not None

    async def test_a_manual_claim_stamps_nothing_and_drops_the_superseded_stamp(
        self, db_session, monkeypatch, scheduled
    ):
        """A manual eject is memory-only by prior ruling (a mid-eject restart degrades to
        an escalation-only hold), and it can SUPERSEDE a hydrated pending — so the
        superseded unit's stamp must go, or the next restart rebuilds a phantom eject
        from it and every later eject 409s ``eject_in_flight``."""
        printer = await _mk_printer(db_session, "PMAN")
        item = await _mk_item(db_session, printer_id=printer.id, stamp=datetime.now(timezone.utc))
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        _gate(printer.id)
        # The record a restart rebuilt from that very stamp...
        plate_occupancy.hydrate_eject(
            printer.id,
            PendingEject("production", None, item.id, dispatched_at=datetime.now(timezone.utc)),
        )
        _wire_persist()

        # ...superseded by an operator's manual sweep.
        assert plate_occupancy.claim_for_eject(printer.id, PendingEject("manual", None, None), Evidence()) is None
        await _drain(scheduled)

        identity = plate_occupancy.eject_identity(printer.id)
        assert identity is not None and identity.purpose == "manual"
        await db_session.refresh(item)
        assert item.eject_dispatched_at is None

    async def test_retiring_the_eject_nulls_every_stamp_on_the_printer(self, db_session, monkeypatch, scheduled):
        """Printer-scoped, not item-scoped: a crash that stamped two rows for one printer
        must not leave an orphan behind to hydrate as a phantom eject."""
        printer = await _mk_printer(db_session, "PCL2")
        now = datetime.now(timezone.utc)
        a = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        b = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        _gate(printer.id)
        plate_occupancy.hydrate_eject(printer.id, PendingEject("production", None, b.id, dispatched_at=now))
        _wire_persist()

        assert plate_occupancy.resolve_eject(printer.id, "completed") is None
        await _drain(scheduled)

        assert plate_occupancy.eject_identity(printer.id) is None
        await db_session.refresh(a)
        await db_session.refresh(b)
        assert a.eject_dispatched_at is None
        assert b.eject_dispatched_at is None

    async def test_an_unverified_resolve_also_drops_the_mirror(self, db_session, monkeypatch, scheduled):
        # The sweep is over either way — only the PLATE differs (it stays gated under
        # EscalationOnly), and a durable stamp for an eject that is no longer in flight
        # would rebuild as a phantom at the next restart.
        printer = await _mk_printer(db_session, "PUNV")
        now = datetime.now(timezone.utc)
        item = await _mk_item(db_session, printer_id=printer.id, stamp=now)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        _gate(printer.id, source="SUB-U")
        plate_occupancy.hydrate_eject(printer.id, PendingEject("production", None, item.id, dispatched_at=now))
        _wire_persist()

        assert plate_occupancy.resolve_eject(printer.id, "unverified") is None
        await _drain(scheduled)

        await db_session.refresh(item)
        assert item.eject_dispatched_at is None
        await db_session.refresh(printer)
        assert printer.awaiting_plate_clear is True  # the plate is still carrying a part
        assert printer.plate_gate_subtask_id == "SUB-U"


class TestDispatchStampsOnDispatch:
    """The full dispatch path stamps the owning unit; a retirement NULLs it; a
    re-dispatch re-stamps. End to end, through the same one persist callable."""

    @staticmethod
    def _make_source_3mf() -> Path:
        fd, name = tempfile.mkstemp(suffix=".gcode.3mf")
        os.close(fd)
        path = Path(name)
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; max_z_height: 18.00\n"
            "; HEADER_BLOCK_END\n"
            "; EXECUTABLE_BLOCK_START\n"
            "G1 X10 Y10\n"
            "; EXECUTABLE_BLOCK_END\n"
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", gcode)
            zf.writestr("3D/3dmodel.model", "<model/>")
        return path

    @staticmethod
    def _dispatch_patches():
        return (
            patch.object(printer_manager, "is_connected", return_value=True),
            patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
            patch("backend.app.services.bambu_ftp.upload_file_async", AsyncMock(return_value=True)),
            patch.object(printer_manager, "start_print", MagicMock(return_value=True)),
        )

    async def test_dispatch_stamps_eject_dispatched_at(self, db_session, monkeypatch, scheduled, seed_geometry):
        source = self._make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "PDISP")
            lib = LibraryFile(
                filename="s.gcode.3mf",
                file_path=str(source),
                file_type="gcode.3mf",
                file_size=source.stat().st_size,
                is_external=True,
            )
            db_session.add(lib)
            await db_session.flush()
            prof = EjectProfile(name="pd-ep")
            db_session.add(prof)
            await db_session.flush()
            item = await _mk_item(db_session, printer_id=printer.id)
            item.library_file_id = lib.id
            item.eject_profile_id = prof.id
            await db_session.commit()
            _patch_session(monkeypatch, db_session)
            _wire_persist()
            _gate(printer.id)

            c1, c2, c3, c4 = self._dispatch_patches()
            with c1, c2, c3, c4:
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            await _drain(scheduled)
            await db_session.refresh(item)
            assert item.eject_dispatched_at is not None
            first_stamp = item.eject_dispatched_at

            # The eject's terminal retires it, and the mirror goes with it.
            assert plate_occupancy.resolve_eject(printer.id, "completed") is None
            await _drain(scheduled)
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None

            # A fresh deposit, a fresh sweep — and a fresh stamp.
            _gate(printer.id)
            c1, c2, c3, c4 = self._dispatch_patches()
            with c1, c2, c3, c4:
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            await _drain(scheduled)
            await db_session.refresh(item)
            assert item.eject_dispatched_at is not None
            assert first_stamp is not None
        finally:
            source.unlink(missing_ok=True)

    async def test_a_refused_dispatch_leaves_no_stamp(self, db_session, monkeypatch, scheduled, seed_geometry):
        # The occupancy gate refuses before the build, so nothing is claimed — and a
        # durable stamp for a sweep that never went out would gate the printer forever.
        source = self._make_source_3mf()
        try:
            printer = await _mk_printer(db_session, "PREF")
            lib = LibraryFile(
                filename="r.gcode.3mf",
                file_path=str(source),
                file_type="gcode.3mf",
                file_size=source.stat().st_size,
                is_external=True,
            )
            db_session.add(lib)
            await db_session.flush()
            prof = EjectProfile(name="pr-ep")
            db_session.add(prof)
            await db_session.flush()
            item = await _mk_item(db_session, printer_id=printer.id)
            item.library_file_id = lib.id
            item.eject_profile_id = prof.id
            await db_session.commit()
            _patch_session(monkeypatch, db_session)
            _wire_persist()
            # No gate raised: the plate is clear, so there is nothing to sweep.

            c1, c2, c3, c4 = self._dispatch_patches()
            with c1, c2, c3, c4, pytest.raises(remote.EjectDispatchError) as exc:
                await remote.dispatch_part_present_eject(
                    db_session, printer_id=printer.id, queue_item_id=item.id, purpose="production", run_id=None
                )
            assert exc.value.code == "not_occupied"
            await _drain(scheduled)
            await db_session.refresh(item)
            assert item.eject_dispatched_at is None
        finally:
            source.unlink(missing_ok=True)


class TestRestartRoundTrip:
    """The mirror's whole purpose: what a live claim WRITES is what a restart REBUILDS.

    The TTL / newest-wins / NULLed-losers decision table is pinned against ``hydrate()``
    directly in ``test_plate_occupancy_store.py``; what is pinned here is the round trip
    itself — a stamp written by the eject lane's own transition, read back by the loader,
    with the provenance the rebuilt record must carry."""

    async def test_a_claims_stamp_rebuilds_as_a_hydrated_pending(self, db_session, monkeypatch, scheduled):
        printer = await _mk_printer(db_session, "PRT")
        item = await _mk_item(db_session, printer_id=printer.id)
        await db_session.commit()
        _patch_session(monkeypatch, db_session)
        _wire_persist()
        _gate(printer.id)

        plate_occupancy.claim_for_eject(
            printer.id,
            PendingEject(
                "production",
                None,
                item.id,
                expected_runtime_s=83.0,
                drop_span_s=30.0,
                sweep_span_s=20.0,
                tail_s=15.0,
            ),
            Evidence(),
        )
        await _drain(scheduled)
        dispatched = plate_occupancy.eject_identity(printer.id).dispatched_at
        assert store._as_utc(dispatched) > datetime.now(timezone.utc) - timedelta(
            hours=store._PENDING_EJECT_STALE_TTL_H
        )

        # The process dies and comes back: only the DB survives.
        plate_occupancy.reset_for_tests()
        await store.hydrate()

        identity = plate_occupancy.eject_identity(printer.id)
        assert identity is not None
        assert (identity.purpose, identity.queue_item_id) == ("production", item.id)
        # Provenance is a property of HOW the record was born: no watchdog can arm on it,
        # and an operator eject SUPERSEDES it rather than being refused by it.
        assert identity.hydrated is True
        assert identity.started_at is None
        assert store._as_utc(identity.dispatched_at) == store._as_utc(dispatched)
        # The build figures are NOT durable — one timestamp column cannot carry them,
        # which is exactly why a rehydrated pending never arms a runtime watchdog.
        rebuilt = plate_occupancy.pending_eject_view(printer.id)
        assert rebuilt.expected_runtime_s is None
        assert rebuilt.drop_span_s is None
        # Every phase budget goes the same way, which is what disarms the sweep and
        # epilogue lanes on a rehydrated pending: no schema carries them, and inventing
        # one would let a post-restart record buy patience the farm cannot justify.
        assert rebuilt.sweep_span_s is None
        assert rebuilt.tail_s is None
