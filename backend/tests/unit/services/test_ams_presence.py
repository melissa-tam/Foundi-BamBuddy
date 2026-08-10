"""Mid-run AMS refill recognition — ams_presence service tests.

Covers the presence-transition tracking (discovery re-read on a gain while idle,
quiet first-push seeding, no auto-unassign on loss) and the print-terminal
reconcile sweep. The prompt/grace machinery (``new_spool_detected``) was deleted —
tagless spools are now auto-minted/configured by ``spool_tagless`` — so those cases
are gone.

Sweep eligibility is NEED-driven (``identify_needed``): tagged slots are refreshed,
physically-changed slots get one discovery read, and an UNTOUCHED tagless slot is
never read — the last being the fix for the standing "failed to read the filament
information" (0700_2X00_0001_0081 / 07XX_4025) errors a commanded read on a tagless
slot can only produce. The old ``data_origin == "ams_auto"`` eligibility rule is gone.
"""

import logging
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence
from backend.app.services.tray_observation import observe_ams_push

_VALID_TAG = "1234567890ABCDEF"

# Captured before any fixture can patch it, so the delegation tests can exercise the
# REAL unit_drying (the autouse fixture below replaces ams_presence.unit_drying).
_REAL_UNIT_DRYING = ams_presence.unit_drying


@pytest.fixture(autouse=True)
def _clean_state():
    ams_presence._reset_state()
    yield
    ams_presence._reset_state()


@pytest.fixture(autouse=True)
def _default_not_drying(monkeypatch):
    """These tests model a NON-drying printer. A bare ``MagicMock`` client returns a
    truthy Mock from ``ams_unit_drying``, which would make the new drying gate read
    every presence/sweep test as drying; default the gate OFF. Drying-specific tests
    re-patch ``ams_presence.unit_drying`` to True, and the delegation tests call
    ``_REAL_UNIT_DRYING`` directly."""
    monkeypatch.setattr(ams_presence, "unit_drying", lambda printer_id, ams_id: False)


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """Point ams_presence's own-session opener (terminal sweep) at the test
    engine — mirrors farm_staging's AMS-hook test fixture."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


def _tray(tray_id, *, state, tray_type="", tag="0000000000000000", tray_uuid="0" * 32, remain=0):
    return {
        "id": tray_id,
        "state": state,
        "tray_type": tray_type,
        "tag_uid": tag,
        "tray_uuid": tray_uuid,
        "remain": remain,
    }


def _obs(printer_id, ams_data):
    """The push payload as OBSERVATIONS — through the real builder, not a hand-rolled
    list. ``printer_manager``'s raw hook calls exactly this, so a fixture that yields
    ``present is None`` here yields ``None`` in production too."""
    return observe_ams_push(printer_id, ams_data)


async def _push(printer_id, ams_data, db):
    """Drive ONE presence pass the way production does (E1): build the push's
    observations, hand them to ``on_tray_observations``. Keeps every existing case's
    payload fixtures and assertions intact — the port's own proof."""
    await ams_presence.on_tray_observations(printer_id, _obs(printer_id, ams_data), db)


def _pstate(trays, *, ams_id=0, gcode_state="IDLE", subtask_id="task-1", ams_status_main=0, tray_now=255):
    # tray_now defaults to 255 (no filament engaged) so every existing caller reads as
    # NOT engaged — the same behaviour a missing attr gave via _filament_engaged's
    # getattr default. Engaged-filament cases pass tray_now=<loaded global tray id>.
    return SimpleNamespace(
        state=gcode_state,
        subtask_id=subtask_id,
        ams_status_main=ams_status_main,
        tray_now=tray_now,
        raw_data={"ams": [{"id": ams_id, "tray": trays}]},
    )


def _arm_cycle(printer_id=1, ams_id=0, tray_id=0, *, age=0.0):
    """Record an unanswered QUALIFIED physical cycle for a slot — what a >=5 s
    pull-and-reseat leaves behind — without replaying the whole presence sequence.
    The end-to-end path (loss → backdated absence → gain) is pinned separately."""
    ams_presence._physical_cycle_at[(printer_id, ams_id, tray_id)] = ams_presence.time.monotonic() - age


def _arm_occasion(printer_id=1, ams_id=0, tray_id=0):
    """Open a READ OCCASION for a slot — the permission every STANDING-evidence arm of
    identify_needed requires (both rfid_refresh arms + the spent-occupied arm). In
    production only a qualified physical cycle or the terminal's between-prints policy
    opens one, and a commanded read consumes it."""
    ams_presence.open_read_occasion(printer_id, ams_id, tray_id)


def _stale_remain_tray(tray_id, **kw):
    """A SEATED (state 10) tray whose wire ``remain`` never landed (-1).

    THE shape the terminal's between-prints policy owes a refresh for: doctrine rule 8
    makes wire remain% the truth for a tagged row, and usage_tracker's W6 ledger-decrease
    repair has nothing to repair from until that read lands. A tagged slot reporting a
    sane remain on its ordinary AMS pushes needs no commanded read at all."""
    kw.setdefault("state", 10)
    kw.setdefault("remain", -1)
    return _tray(tray_id, **kw)


async def _physically_cycle(db_session, printer_id=1, ams_id=0, tray_id=0, *, tray=None):
    """Drive a REAL qualified physical cycle through on_tray_observations: seed present,
    observe the loss, backdate the absence past _MIN_PHYSICAL_ABSENT_S, then gain."""
    seated = tray if tray is not None else _tray(tray_id, state=11)
    await _push(printer_id, [{"id": ams_id, "tray": [seated]}], db_session)  # seed present
    await _push(printer_id, [{"id": ams_id, "tray": [_tray(tray_id, state=9)]}], db_session)  # pulled
    ams_presence._absent_since[(printer_id, ams_id, tray_id)] = ams_presence.time.monotonic() - (
        ams_presence._MIN_PHYSICAL_ABSENT_S + 1
    )
    await _push(printer_id, [{"id": ams_id, "tray": [seated]}], db_session)  # reseated


def _patch_pm(monkeypatch, *, status=None, client=None):
    monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
    monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)


class TestPresenceTracking:
    """on_tray_observations presence transitions (steady state, after the first push)."""

    async def test_gain_while_idle_rereads(self, db_session, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        # First push primes (quiet — no re-read even though the slot is present).
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        client.ams_refresh_tray.assert_not_called()

        # Second push: physical insert 9→11 while idle → immediate re-read.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_first_push_seeds_quietly(self, db_session, monkeypatch):
        # A present-but-unidentified slot on the very first push must NOT re-read
        # (a refill done while the server was down is seeded, not acted on).
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        client.ams_refresh_tray.assert_not_called()

    async def test_gain_during_print_takes_no_action(self, db_session, monkeypatch):
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="RUNNING"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        client.ams_refresh_tray.reset_mock()
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain mid-print
        client.ams_refresh_tray.assert_not_called()  # ams_get_rfid never fired during a print

    async def test_no_rereads_without_gain(self, db_session, monkeypatch):
        # Already-present slot that stays present → no re-read (no rising edge).
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # prime present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # still present
        client.ams_refresh_tray.assert_not_called()

    async def test_presence_loss_keeps_assignment(self, db_session, monkeypatch):
        # A spool pulled for drying keeps its assignment — NO silent auto-unassign.
        db_session.add(SpoolAssignment(spool_id=1, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        from sqlalchemy import select

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # prime present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss

        res = await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == 1))
        assert res.scalar_one_or_none() is not None  # assignment survived the removal


class TestRawLaneEdges:
    """E1 (2026-08-07, 001-H2S slot 1): presence edges are derived from the RAW
    observation stream, so they inherit its TRI-STATE presence and its per-push
    honesty. These three pin what the merged lane could not express."""

    @pytest.fixture(autouse=True)
    def _inert_side_effects(self, monkeypatch):
        """Keep the gain block's awaited collaborators inert so a case asserts on the
        LEDGERS, not on their downstream sessions. ``command_identify`` is stubbed too:
        the real one CONSUMES the read occasion the gain just opened, and whether the
        occasion opened is precisely what these cases measure."""
        from backend.app.services import spool_tagless

        note = AsyncMock()
        identify = AsyncMock()
        monkeypatch.setattr(spool_tagless, "note_physical_cycle", note)
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        monkeypatch.setattr("backend.app.services.spool_binding.stamp_loaded_for_slot", AsyncMock())
        monkeypatch.setattr(ams_presence, "command_identify", identify)
        return SimpleNamespace(note=note, identify=identify)

    async def test_first_sight_gain_opens_an_occasion(self, db_session, monkeypatch, _inert_side_effects):
        """A slot the boot batch seeded ABSENT, later asserting a bare seated tray, is a
        genuine gain whose absence START was never observed (``absent_for is None``).

        THE INCIDENT'S INSERT SHAPE. It takes the WIDER ``qualified`` tier — the read
        occasion opens and the qualified cycle is banked, so the identify lanes can buy
        the one read that names the roll — while the STRICT ``physical_cycle`` tier stays
        withheld, because minting a spool row or prompting the operator off an UNMEASURED
        absence is the expensive mistake. Under the merged lane this gain fired nothing at
        all (the boot-seeding hole)."""
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # boot batch: seeded ABSENT
        assert ams_presence._last_presence[(1, 0, 0)] is False

        # The operator's insert: state 10 (seated), bare — no tag, nothing to match on.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=10)]}], db_session)

        assert ams_presence._absent_since.get((1, 0, 0)) is None  # no measured absence existed
        assert (1, 0, 0) in ams_presence._physical_cycle_at  # qualified tier banked
        assert (1, 0, 0) in ams_presence._read_occasion_at  # ... and it bought the read
        _inert_side_effects.note.assert_not_awaited()  # strict tier withheld: no mint, no prompt
        # The occasion is spent productively: the idle lane asks for the discovery read.
        _inert_side_effects.identify.assert_awaited_once()
        assert _inert_side_effects.identify.await_args.kwargs["reason"] == "discovery"

    async def test_presence_unknown_never_edges(self, db_session, monkeypatch, _inert_side_effects):
        """``present is None`` is silence, not emptiness: it derives NO edge and leaves
        ``_last_presence`` standing.

        The merged lane answered a BOOLEAN for every tray, so a reduced mid-print push
        (no parseable ``state``) read as "absent" and manufactured a loss edge out of
        nothing — after which the roll's real re-appearance read as a fresh gain. Both
        halves are pinned here."""
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="RUNNING"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # steady (past the seed)
        assert ams_presence._last_presence[(1, 0, 0)] is True

        # The reduced shape: the push carries the slot but asserts nothing about presence.
        reduced = [{"id": 0, "tray": [{"id": 0, "remain": 42}]}]
        assert _obs(1, reduced)[0].present is None  # the fixture really is UNKNOWN
        await _push(1, reduced, db_session)

        assert ams_presence._last_presence[(1, 0, 0)] is True  # untouched — no loss edge
        assert ams_presence._absent_since.get((1, 0, 0)) is None

        # ... so the roll's next ordinary push is NOT a gain either (nothing was lost).
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        assert (1, 0, 0) not in ams_presence._physical_cycle_at
        assert (1, 0, 0) not in ams_presence._gain_at
        _inert_side_effects.identify.assert_not_awaited()

    async def test_pull_and_reseat_banks_a_measured_cycle(self, db_session, monkeypatch, _inert_side_effects):
        """The operator's pull+reinsert — the edge pair the merged lane was blind to for
        38 minutes — banks the STRICT measured cycle off the raw stream.

        The absence is backdated past ``_MIN_PHYSICAL_ABSENT_S`` (this file's clock idiom,
        shared with ``_physically_cycle`` and ``TestPhysicalCycleNote``) rather than slept."""
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # pulled — cleared shape
        assert (1, 0, 0) in ams_presence._absent_since  # the loss edge the merged lane missed

        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # reseated

        _inert_side_effects.note.assert_awaited_once_with(1, 0, 0)  # STRICT tier: measured swap
        assert (1, 0, 0) in ams_presence._physical_cycle_at
        assert (1, 0, 0) in ams_presence._read_occasion_at


class TestOutOfRotationClear:
    """on_tray_observations fires spool_recovery.clear_on_reinsert on a presence GAIN
    edge (physical re-insert), NOT on the first-push seed, and NOT idle-gated."""

    async def test_gain_edge_invokes_clear(self, db_session, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        # Prime absent (state 9), then physical insert 9→11 → clear fires once.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        spy.assert_not_awaited()
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        spy.assert_awaited_once()
        args = spy.await_args.args
        assert args[0] is db_session and args[1] == 1 and args[2] == 0 and args[3] == 0
        assert args[4]["state"] == 11  # the live tray payload

    async def test_first_push_seed_does_not_clear(self, db_session, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)

        # A spool present on the very first push is a seed, not a re-insert.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        spy.assert_not_awaited()

    async def test_gain_during_print_still_clears(self, db_session, monkeypatch):
        # NOT idle-gated: a spool untangled and re-seated mid-print clears too,
        # even though the idle-only RFID re-read stays suppressed during a print.
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="RUNNING"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
        spy.assert_awaited_once()
        client.ams_refresh_tray.assert_not_called()  # idle re-read still suppressed mid-print


class TestTerminalSweep:
    """on_printer_terminal commands exactly the identifies identify_needed asks for:
    tagged slots whose between-prints policy opens a read occasion (wire remain missing
    / ledger past label / spent-latched under an unidentified roll), physically-changed
    slots once, and NOTHING for an untouched tagless slot — once per terminal
    transition."""

    async def test_reads_tagged_and_changed_slots_only(self, db_session, sessions, monkeypatch):
        # (0,1) bound to an auto-minted TAGLESS spool + physically cycled → discovery.
        auto_spool = Spool(material="PETG", data_origin="ams_auto")
        # (0,2) bound to a spool that carries an RFID identity, wire remain missing →
        # refreshed through the DB-bound arm (the tray shows no tag: the incomplete RFID
        # read is exactly why its remain never landed).
        tagged_spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        # (0,3) the same shape with the tag ALSO visible on the wire → the live-tag arm.
        live_spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid="FEDCBA0987654321")
        db_session.add_all([auto_spool, tagged_spool, live_spool])
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=auto_spool.id, printer_id=1, ams_id=0, tray_id=1))
        db_session.add(SpoolAssignment(spool_id=tagged_spool.id, printer_id=1, ams_id=0, tray_id=2))
        db_session.add(SpoolAssignment(spool_id=live_spool.id, printer_id=1, ams_id=0, tray_id=3))
        await db_session.commit()
        _arm_cycle(1, 0, 1)

        order: list[str] = []
        client = MagicMock()
        client.ams_refresh_tray.side_effect = lambda a, t: order.append(f"read {a},{t}") or (True, "ok")
        client.wait_ams_settle = AsyncMock(side_effect=lambda: order.append("settle"))
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            _tray(0, state=11),  # SKIP: untouched tagless — the 0081 factory
                            _tray(1, state=11),  # discovery: tagless-bound, physically cycled
                            _stale_remain_tray(2),  # rfid_refresh: DB-bound tagged, remain never landed
                            _stale_remain_tray(3, tag="FEDCBA0987654321"),  # rfid_refresh: live tag, remain missing
                        ],
                    },
                    {"id": 1, "tray": [_tray(0, state=0)]},  # skip: state 0 excluded
                ]
            },
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)

        assert [c.args for c in client.ams_refresh_tray.call_args_list] == [(0, 1), (0, 2), (0, 3)]
        # Settle-wait is awaited once per swept slot, before each read (including the
        # FIRST) — the pace is per-slot, and it also gives the firmware's own auto-read
        # a chance to land before we command one.
        assert client.wait_ams_settle.await_count == 3
        assert order == ["settle", "read 0,1", "settle", "read 0,2", "settle", "read 0,3"]

    async def test_untouched_tagless_slots_are_never_read(self, db_session, sessions, monkeypatch):
        # THE 0081-factory pin: a full AMS of tagless spools nobody has touched must
        # produce ZERO ams_get_rfid at print end, no matter how many prints end. Each
        # such read fails ("no tag to read") and raises a standing HMS that can never
        # self-clear on a tagless slot — the live 004/011/012 defect.
        bound = Spool(material="PETG", data_origin="ams_auto")
        db_session.add(bound)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=bound.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()

        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(i, state=11) for i in range(4)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        for i in range(3):  # three consecutive prints end
            status.subtask_id = f"t{i}"
            await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()
        assert ams_presence._discovery_read_at == {}

    async def test_state9_included_for_a_changed_slot(self, db_session, sessions, monkeypatch):
        # state 9 stays eligible — a mid-print refill sometimes reads 9 until re-read —
        # but only WITH change evidence; state 0/None is never acted on.
        _arm_cycle(1, 0, 0)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=9)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_discovery_evidence_is_consumed_by_the_read(self, db_session, sessions, monkeypatch):
        # ONE discovery read per change: the next terminal with no NEW physical cycle
        # commands nothing (this is what stops the per-print-end read storm).
        _arm_cycle(1, 0, 0)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        assert client.ams_refresh_tray.call_count == 1

        ams_presence._echo_pending.clear()  # a whole print elapsed; the identify is long done
        status.subtask_id = "t2"
        await ams_presence.on_printer_terminal(1)
        assert client.ams_refresh_tray.call_count == 1  # evidence consumed — no second read

        # A NEW physical cycle re-arms discovery for the following terminal.
        _arm_cycle(1, 0, 0)
        status.subtask_id = "t3"
        await ams_presence.on_printer_terminal(1)
        assert client.ams_refresh_tray.call_count == 2

    async def test_firmware_answer_during_settle_cancels_the_discovery_read(self, db_session, sessions, monkeypatch):
        # The settle wait exists so the firmware's own auto-read lands first. If it
        # answers with a tag while we wait, the discovery read has nothing left to
        # find out — command nothing (the next terminal refreshes it as a tagged slot).
        _arm_cycle(1, 0, 0)
        tray = _tray(0, state=11)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [tray]}]},
        )
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(side_effect=lambda: tray.update(tag_uid=_VALID_TAG))
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()
        assert ams_presence._unanswered_cycle(1, 0, 0) is False  # the firmware's answer counts

    async def test_once_per_transition_dedup(self, db_session, sessions, monkeypatch):
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            # Live tag + wire remain missing → the between-prints policy owes a refresh
            # at every terminal (one per terminal, not one per pass).
            raw_data={"ams": [{"id": 0, "tray": [_stale_remain_tray(0, tag=_VALID_TAG)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        await ams_presence.on_printer_terminal(1)  # duplicate terminal callback, same subtask
        assert client.ams_refresh_tray.call_count == 1

        # A whole print cycle (minutes) elapses before the next terminal, so the
        # prior sweep's identify has long completed and its in-flight echo flag aged
        # out — Guard 3d skips only a STILL-in-flight identify, not a new sweep.
        ams_presence._echo_pending.clear()
        status.subtask_id = "t2"  # a NEW print reached terminal
        await ams_presence.on_printer_terminal(1)
        assert client.ams_refresh_tray.call_count == 2

    async def test_no_eligible_slots_no_reads(self, db_session, sessions, monkeypatch):
        client = MagicMock()
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=0)]}]},  # unknown dialect only
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()
        client.wait_ams_settle.assert_not_awaited()


class TestEchoConsume:
    """The one-shot echo-consume flag. A commanded re-read on a PRESENT slot makes
    the firmware flap the tray state present→9→present (~20 s); that settle-back
    arrives as a fresh gain — the command's own echo — which on_tray_observations would
    otherwise answer with ANOTHER re-read (a self-sustaining ~22 s loop). The flag
    lets the NEXT gain be recognized and swallowed exactly once, with NO time gate
    on genuine physical insertions (empty slots never arm)."""

    async def test_echo_swallowed_exactly_once(self, db_session, monkeypatch):
        # A present untagged slot's re-read arms the flag; the identify flap's
        # settle-back gain is swallowed once (no 2nd re-read, no feed-fault clear);
        # a later genuine flap re-reads again — proving no lingering suppression.
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        # Prime absent, then a genuine insert 9→11 → re-read fires + flag armed.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        assert client.ams_refresh_tray.call_count == 1
        assert clear.await_count == 1
        assert (1, 0, 0) in ams_presence._echo_pending  # armed on success

        # Identify flap: loss 11→9 then settle-back 9→11 — THIS gain is our echo.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        assert client.ams_refresh_tray.call_count == 1  # echo swallowed — no 2nd re-read
        assert clear.await_count == 1  # feed-fault clear NOT re-run for the echo
        assert (1, 0, 0) not in ams_presence._echo_pending  # flag consumed

        # A SECOND genuine pull+reseat afterwards is acted on normally (no lingering
        # gate). Its absence is backdated past _MIN_PHYSICAL_ABSENT_S: only a real
        # physical cycle is discovery evidence, a sub-second state flap is not.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        assert client.ams_refresh_tray.call_count == 2
        assert clear.await_count == 2

    async def test_empty_slot_never_arms(self, db_session, monkeypatch):
        # A re-read commanded on an EMPTY (state 9) slot produces no identify flap,
        # so record_reread must NOT arm — a real insertion made right after a print
        # ends is then recognized instantly, with no swallow.
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        # Directly: an empty slot never arms the flag.
        ams_presence.record_reread(1, 0, 0)
        assert ams_presence._echo_pending == {}

        # A real insertion gain moments later fires the re-read immediately.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # insert
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_terminal_sweep_ignition_killed(self, db_session, sessions, monkeypatch):
        # The loop's ignition: the terminal sweep's discovery read on a changed slot
        # arms the flag; the identify flap's echo gain is then swallowed, so the sweep
        # issues exactly ONE command instead of looping every ~22 s.
        _arm_cycle(1, 0, 0)
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = _pstate([_tray(0, state=11)], gcode_state="FINISH", subtask_id="t1")
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        # Terminal sweep on the present untagged slot → one re-read + flag armed.
        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)
        assert (1, 0, 0) in ams_presence._echo_pending

        # The identify flap's settle-back gain is the sweep command's echo.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
        client.ams_refresh_tray.assert_called_once_with(0, 0)  # still ONE — echo swallowed, loop dead
        assert (1, 0, 0) not in ams_presence._echo_pending

    async def test_valid_tag_gain_skips_reread_but_clears(self, db_session, monkeypatch):
        # A genuine gain on a tray that already carries a valid tag needs no re-read
        # (re-reading would only re-flap it), but the feed-fault clear still runs.
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(
            1, [{"id": 0, "tray": [_tray(0, state=11, tag=_VALID_TAG)]}], db_session
        )  # genuine gain, already identified
        client.ams_refresh_tray.assert_not_called()  # no re-read for an identified tray
        clear.assert_awaited_once()  # feed-fault clear still runs
        assert ams_presence._echo_pending == {}  # nothing to arm (no command issued)

    async def test_refused_command_arms_nothing(self, db_session, monkeypatch):
        # A refused re-read (client returns (False, ...) when filament is loaded)
        # starts no identify cycle → no echo → the flag must NOT arm, and a following
        # gain still fires a fresh re-read attempt.
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (False, "Please unload filament first")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain (refused)
        assert client.ams_refresh_tray.call_count == 1
        assert ams_presence._echo_pending == {}  # refused → nothing armed

        # A following physical cycle still attempts a re-read (no phantom suppression):
        # a refused command learned nothing, so the change is still unanswered.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
        assert client.ams_refresh_tray.call_count == 2

    async def test_stale_flag_treated_genuine(self, db_session, monkeypatch):
        # A flag whose identify cycle never ran (command lost to a race) is GC'd:
        # once older than _ECHO_PENDING_STALE_S it reads as no-flag, so the gain is
        # treated genuine — re-read fires and the feed-fault clear runs. The flag is
        # arm-aged directly (real monotonic, minus the bound) rather than freezing
        # the process-wide time.monotonic, which is the async event loop's clock.
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        # Arm the flag with a timestamp already older than the staleness bound.
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic() - ams_presence._ECHO_PENDING_STALE_S - 1

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # stale gain

        client.ams_refresh_tray.assert_called_once_with(0, 0)  # stale flag → genuine → re-read fires
        clear.assert_awaited_once()  # and the feed-fault clear runs


class TestIdentifyNeeded:
    """identify_needed is the single eligibility authority. Doctrine: a commanded RFID
    read on a slot with no tag can only FAIL, and the resulting HMS can never
    self-clear on a tagless slot — so a slot is read only when the read can succeed
    (a tag is there) or when something changed and the failure is itself the answer."""

    async def _needed(self, db_session, tray, *, tray_id=0):
        return await ams_presence.identify_needed(db_session, 1, 0, tray_id, tray, False)

    async def test_live_tagged_is_refreshed(self, db_session):
        # remain% for gram tracking + reused-core detection ride on this read — but only
        # against an OPEN occasion (a qualified cycle, or the terminal's policy).
        _arm_occasion()
        assert await self._needed(db_session, _tray(0, state=11, tag=_VALID_TAG)) == "rfid_refresh"

    async def test_live_tagged_without_an_occasion_is_not_read(self, db_session):
        # THE storm pin, live-tag arm: a tagged tray publishes its remain on every
        # ordinary AMS push, so re-deriving "it is tagged" every pass is not a read reason.
        assert await self._needed(db_session, _tray(0, state=11, tag=_VALID_TAG)) is None

    async def test_db_bound_tagged_is_refreshed(self, db_session):
        _arm_occasion()
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        assert await self._needed(db_session, _tray(0, state=11)) == "rfid_refresh"

    async def test_db_bound_tagged_without_an_occasion_is_not_read(self, db_session):
        # THE storm pin (2026-08-07, printers 6/10/…): a DB binding whose tagged spool the
        # tray does not show is a STANDING condition. Re-derived on every reconcile /
        # pipeline pass it owed a read every ~31 s forever, each command holding the
        # printer's identify gate so the slot pipeline deferred every decision on it.
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        assert await self._needed(db_session, _tray(0, state=11)) is None

    async def test_db_bound_tagged_but_absent_is_not_read(self, db_session):
        # The bound spool was pulled: a read of an empty slot fails exactly like a
        # tagless one and raises the same never-clearing 0081. Presence is required.
        spool = Spool(material="PETG", data_origin="rfid_auto", tray_uuid="A" * 32)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        assert await self._needed(db_session, _tray(0, state=9)) is None

    async def test_untouched_tagless_bound_slot_is_not_read(self, db_session):
        spool = Spool(material="PETG", data_origin="ams_auto")
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        assert await self._needed(db_session, _tray(0, state=11)) is None

    async def test_unassigned_untouched_slot_is_not_read(self, db_session):
        assert await self._needed(db_session, _tray(0, state=11)) is None

    async def test_changed_untagged_slot_is_discovery(self, db_session):
        _arm_cycle(1, 0, 0)
        assert await self._needed(db_session, _tray(0, state=11)) == "discovery"

    async def test_changed_slot_bound_to_a_tagged_spool_is_discovery(self, db_session):
        # Something physically moved: the DB's idea of what is in the slot is now a
        # hypothesis, so the read is treated as one that may legitimately fail.
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        _arm_cycle(1, 0, 0)
        assert await self._needed(db_session, _tray(0, state=11)) == "discovery"

    async def test_unknown_dialect_state_is_never_read(self, db_session):
        _arm_cycle(1, 0, 0)
        assert await self._needed(db_session, _tray(0, state=0)) is None

    async def test_answered_cycle_is_no_longer_evidence(self, db_session):
        _arm_cycle(1, 0, 0, age=5)
        ams_presence.note_identity_learned(1, 0, 0)  # firmware answered / we read it
        assert await self._needed(db_session, _tray(0, state=11)) is None

    def test_cycle_accessors_are_non_consuming(self):
        _arm_cycle(1, 0, 0)
        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0
        ams_presence.note_identity_learned(1, 0, 0)
        assert ams_presence._unanswered_cycle(1, 0, 0) is False  # evidence spent …
        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0  # … stamp survives
        assert ams_presence.last_physical_cycle_age(1, 0, 3) is None
        assert ams_presence.recent_gain_age(1, 0, 3) is None


class TestDiscoveryFailureSuppression:
    """A discovery read asks a slot that may have no tag. The firmware answers a
    missing tag with "Failed to read the filament information … the AMS main board may
    be malfunctioning" (0700_2X00_0001_0081 / 07XX_4025). That is the ANSWER, not a
    fault: suppressed farm-side. An UNCOMMANDED one still notifies."""

    _READ_FAIL_CODE = 0x00010081
    _ATTR_SLOT0 = 0x07002000
    _ATTR_SLOT2 = 0x07002200

    async def test_desiccant_cycle_yields_one_suppressed_discovery_read(self, db_session, sessions, monkeypatch):
        # THE desiccant pin. The operator pulls a tagless spool for >5 s to top up the
        # desiccant and puts the SAME spool back mid-print. Cost to the operator: ONE
        # discovery read at the next print end and ZERO notifications.
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        monkeypatch.setattr("backend.app.services.spool_tagless.note_physical_cycle", AsyncMock())
        spool = Spool(material="PETG", data_origin="ams_auto")
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()

        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        printing = _pstate([_tray(0, state=11)], gcode_state="RUNNING", subtask_id="t1")
        _patch_pm(monkeypatch, status=printing, client=client)

        await _physically_cycle(db_session)  # pulled + reseated DURING the print
        client.ams_refresh_tray.assert_not_called()  # never mid-print

        finish = _pstate([_tray(0, state=11)], gcode_state="FINISH", subtask_id="t1")
        _patch_pm(monkeypatch, status=finish, client=client)
        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)  # exactly ONE discovery read

        # …and the read's failure is recognized as its own answer, not a fault.
        assert ams_presence.is_expected_read_failure(1, self._ATTR_SLOT0, self._READ_FAIL_CODE) is True

        # A second print ending with no new cycle commands nothing at all.
        ams_presence._echo_pending.clear()
        finish.subtask_id = "t2"
        await ams_presence.on_printer_terminal(1)
        assert client.ams_refresh_tray.call_count == 1

    def test_uncommanded_read_failure_still_notifies(self):
        # Nobody asked this slot anything — a real reader fault must surface.
        assert ams_presence.is_expected_read_failure(1, self._ATTR_SLOT0, self._READ_FAIL_CODE) is False

    def test_other_slot_read_failure_still_notifies(self):
        ams_presence._discovery_read_at[(1, 0, 0)] = ams_presence.time.monotonic()
        assert ams_presence.is_expected_read_failure(1, self._ATTR_SLOT2, self._READ_FAIL_CODE) is False

    def test_other_printer_read_failure_still_notifies(self):
        ams_presence._discovery_read_at[(1, 0, 0)] = ams_presence.time.monotonic()
        assert ams_presence.is_expected_read_failure(2, self._ATTR_SLOT0, self._READ_FAIL_CODE) is False

    def test_expired_window_still_notifies(self):
        ams_presence._discovery_read_at[(1, 0, 0)] = (
            ams_presence.time.monotonic() - ams_presence._DISCOVERY_READ_WINDOW_S - 1
        )
        assert ams_presence.is_expected_read_failure(1, self._ATTR_SLOT0, self._READ_FAIL_CODE) is False

    def test_slotless_4025_matches_the_same_ams_unit(self):
        # 07XX_4025 names the AMS unit but no slot — matched against a fresh discovery
        # read on that unit; a different unit still notifies.
        ams_presence._discovery_read_at[(1, 0, 2)] = ams_presence.time.monotonic()
        assert ams_presence.is_expected_read_failure(1, 0x07000000, 0x00004025) is True
        assert ams_presence.is_expected_read_failure(1, 0x07010000, 0x00004025) is False

    def test_non_read_failure_codes_are_never_suppressed(self):
        ams_presence._discovery_read_at[(1, 0, 0)] = ams_presence.time.monotonic()
        assert ams_presence.is_expected_read_failure(1, self._ATTR_SLOT0, 0x00020001) is False  # runout
        assert ams_presence.is_expected_read_failure(1, 0x07008210, 0x00008010) is False  # feed fault

    async def test_rfid_refresh_read_failure_is_not_suppressed(self, monkeypatch):
        # Only DISCOVERY reads stamp. A slot we believed to be TAGGED failing to read
        # is a genuine fault report and must reach the operator.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11, tag=_VALID_TAG)]), client=client)

        ok, _ = await ams_presence.command_identify(1, 0, 0, source="terminal_sweep", reason="rfid_refresh")
        assert ok is True
        assert ams_presence._discovery_read_at == {}
        assert ams_presence.is_expected_read_failure(1, self._ATTR_SLOT0, self._READ_FAIL_CODE) is False


class TestManualRefreshBypass:
    """The operator's manual refresh bypasses NEED — never wire safety."""

    async def test_bypass_commands_a_read_with_no_need(self, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)

        ok, _msg = await ams_presence.command_identify(1, 0, 0, source="manual_refresh", enforce_need=False)
        assert ok is True
        client.ams_refresh_tray.assert_called_once_with(0, 0)
        assert (1, 0, 0) in ams_presence._echo_pending  # same bookkeeping as every read
        # An operator read is not a discovery read: its failure is NOT suppressed.
        assert ams_presence._discovery_read_at == {}

    async def test_need_enforced_without_a_reason_commands_nothing(self, monkeypatch):
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)

        ok, msg = await ams_presence.command_identify(1, 0, 0, source="terminal_sweep")
        assert ok is False and "not evaluated" in msg  # fail-closed without a session
        client.ams_refresh_tray.assert_not_called()

    async def test_need_resolved_from_db_when_no_reason_passed(self, db_session, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)

        ok, msg = await ams_presence.command_identify(1, 0, 0, source="idle_gain", db=db_session)
        assert ok is False and msg == "no identify needed"  # untouched tagless slot

        _arm_cycle(1, 0, 0)
        ok, _ = await ams_presence.command_identify(1, 0, 0, source="idle_gain", db=db_session)
        assert ok is True
        assert (1, 0, 0) in ams_presence._discovery_read_at

    async def test_client_refusal_is_returned_unchanged(self, monkeypatch):
        # Wire safety stays with the client: a drying / identifying refusal reaches the
        # operator verbatim, and nothing is stamped for a read that never went out.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (False, "AMS unit is drying — retry after the drying cycle")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)

        ok, msg = await ams_presence.command_identify(1, 0, 0, source="manual_refresh", enforce_need=False)
        assert ok is False and "drying" in msg
        assert ams_presence._echo_pending == {}
        assert ams_presence._slot_read_at == {}

    async def test_no_client_is_reported(self, monkeypatch):
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=None)
        ok, msg = await ams_presence.command_identify(1, 0, 0, source="manual_refresh", enforce_need=False)
        assert ok is False and msg == "Printer not connected"


class TestIdentifyInFlight:
    """identify_in_flight: read-only 'is a commanded identify (or an active unit
    identify) still running on this slot?' — the single signal Guards 3d and 4
    share to keep at most one identify per slot in flight."""

    def test_unit_busy_any_tray_is_true(self, monkeypatch):
        # ams_status_main == 2 (the unit is actively identifying) → True for the slot.
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(ams_status_main=2))
        assert ams_presence.identify_in_flight(1, 0, 0) is True

    def test_fresh_flag_is_true(self, monkeypatch):
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(ams_status_main=0))
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic()
        assert ams_presence.identify_in_flight(1, 0, 0) is True

    def test_stale_flag_is_false(self, monkeypatch):
        # A flag older than _IDENTIFY_ACTIVE_S no longer implies an in-flight identify.
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(ams_status_main=0))
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic() - ams_presence._IDENTIFY_ACTIVE_S - 1
        assert ams_presence.identify_in_flight(1, 0, 0) is False

    def test_neither_is_false(self, monkeypatch):
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(ams_status_main=0))
        assert ams_presence.identify_in_flight(1, 0, 0) is False

    def test_no_status_is_false(self, monkeypatch):
        # get_status None (printer gone) → getattr default 0 → not busy, no flag → False.
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: None)
        assert ams_presence.identify_in_flight(1, 0, 0) is False


class TestTerminalSweepIdentifySkip:
    """Guard 3d: the terminal sweep skips a slot whose identify is already in
    flight (fresh _echo_pending) so a concurrent idle-gain re-read is never
    doubled, but still sweeps a slot whose flag has gone stale."""

    async def test_skips_fresh_flag_sweeps_stale(self, db_session, sessions, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11), _tray(1, state=11)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        _arm_cycle(1, 0, 0)  # both slots NEED a discovery read …
        _arm_cycle(1, 0, 1)
        now = ams_presence.time.monotonic()
        ams_presence._echo_pending[(1, 0, 0)] = now  # … fresh flag → T0 skipped anyway
        ams_presence._echo_pending[(1, 0, 1)] = now - ams_presence._IDENTIFY_ACTIVE_S - 1  # stale → T1 swept

        await ams_presence.on_printer_terminal(1)
        assert [c.args for c in client.ams_refresh_tray.call_args_list] == [(0, 1)]  # only the stale slot


class TestIdentifyCollisionRegression:
    """Incident regression: an idle-gain re-read, the terminal sweep, and the
    tagless config used to hit one slot within seconds; the second identify / the
    filament-setting write failed the firmware's in-flight read (HMS
    0700_2x00_0001_0081). Now exactly one identify is issued and the tagless path
    defers while it is in flight — no filament-setting write in the window."""

    async def test_gain_reread_then_sweep_and_config_do_not_collide(self, db_session, sessions, monkeypatch):
        from sqlalchemy import func, select

        from backend.app.models.spool import Spool
        from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
        from backend.app.services.slot_state import DecisionKind
        from backend.app.services.tray_observation import observe_tray

        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())

        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        present = _pstate([_tray(0, state=11)], gcode_state="IDLE", ams_status_main=0)
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: present)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        # (1) Idle gain 9→11 → exactly ONE identify command; the in-flight flag arms.
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain → re-read
        assert client.ams_refresh_tray.call_count == 1  # identify #1
        assert (1, 0, 0) in ams_presence._echo_pending

        # (2) Terminal sweep on the SAME slot must SKIP it — no second identify — even
        # though a further physical cycle lands while that identify is still running.
        _arm_cycle(1, 0, 0)
        finish = SimpleNamespace(
            state="FINISH",
            subtask_id="task-9",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: finish)
        await ams_presence.on_printer_terminal(1)
        assert client.ams_refresh_tray.call_count == 1  # sweep skipped — still ONE identify total

        # (3) The slot pipeline on the SAME slot must DEFER — nothing minted, no write.
        # The real client arms its per-printer identify gate the moment it publishes an
        # ams_get_rfid, and reports that as the "identify_in_flight" write refusal; the
        # pipeline reads that refusal as row 1 of the decision table.
        client.ams_write_refusal.return_value = "identify_in_flight"
        client.ams_unit_drying.return_value = False
        tray = {
            "id": 0,
            "state": 11,
            "tray_type": "PETG",
            "tray_sub_brands": "PETG HF",
            "tray_color": "112233FF",
            "tray_info_idx": "",
            "tray_weight": "0",
            "tag_uid": "0" * 16,
            "tray_uuid": "0" * 32,
            "remain": 40,
        }

        async def _no_settings(_key):
            return None

        transitions = await run_slot_pipeline(
            1,
            [observe_tray(1, 0, tray)],
            PipelineDeps(db=db_session, client=client, get_setting=_no_settings),
        )
        assert [t.decision.kind for t in transitions] == [DecisionKind.DEFER]
        assert transitions[0].decision.reason == "identify_in_flight"
        minted = await db_session.scalar(select(func.count(Spool.id)))
        assert minted == 0  # zero mints / filament-setting writes during the in-flight window


class TestUnitDryingDelegation:
    """unit_drying delegates to the client's ams_unit_drying (single origin) and is
    crash-safe when the printer is gone or the client raises."""

    def test_delegates_to_client(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)
        client.ams_unit_drying.return_value = True
        assert _REAL_UNIT_DRYING(1, 0) is True
        client.ams_unit_drying.return_value = False
        assert _REAL_UNIT_DRYING(1, 0) is False

    def test_no_client_is_false(self, monkeypatch):
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: None)
        assert _REAL_UNIT_DRYING(1, 0) is False

    def test_client_raises_is_false(self, monkeypatch):
        client = MagicMock()
        client.ams_unit_drying.side_effect = RuntimeError("boom")
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)
        assert _REAL_UNIT_DRYING(1, 0) is False


class TestDryingGates:
    """A drying cycle flaps tray presence (state → 10) with no physical event. While
    drying, on_tray_observations must NOT clear a feed-fault flag and NOT fire an idle
    re-read, and the terminal sweep must skip the drying unit — a re-read would
    disengage the tray and fail the cycle (HMS 0700_C069)."""

    async def test_clear_on_reinsert_skipped_while_drying(self, db_session, monkeypatch):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # drying flap gain
        spy.assert_not_awaited()  # feed-fault clear NOT run for a drying flap
        assert ams_presence._last_presence[(1, 0, 0)] is True  # presence map still updated

    async def test_idle_reread_skipped_while_drying(self, db_session, monkeypatch):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # drying flap
        client.ams_refresh_tray.assert_not_called()  # no re-read during drying

    async def test_terminal_sweep_skips_drying_unit(self, db_session, sessions, monkeypatch):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        _arm_cycle(1, 0, 0)  # the slot NEEDS a discovery read — drying is what stops it
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()  # drying unit skipped

    async def test_non_drying_control_still_reads(self, db_session, monkeypatch):
        # Control (autouse unit_drying=False): the same gain re-reads normally.
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        client.ams_refresh_tray.assert_called_once_with(0, 0)


class TestTrayPresentSingleOrigin:
    """_tray_present is keyed off bambu_mqtt.TRAY_PRESENT_STATES (one origin)."""

    def test_single_origin_and_membership(self):
        from backend.app.services.bambu_mqtt import TRAY_PRESENT_STATES

        assert ams_presence.TRAY_PRESENT_STATES is TRAY_PRESENT_STATES
        assert ams_presence._tray_present({"state": 10}) is True
        assert ams_presence._tray_present({"state": 11}) is True
        assert ams_presence._tray_present({"state": 9}) is False
        assert ams_presence._tray_present({"state": 0}) is False
        assert ams_presence._tray_present({"state": None}) is False


class TestEchoWindowBoundary:
    """F3: the echo-consume window equals the identify-cycle bound (30 s). A gain
    within it is the identify flap's echo (swallowed); beyond it a gain is a real
    reseat and runs clear_on_reinsert."""

    async def test_echo_swallowed_under_30s(self, db_session, monkeypatch):
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        # Arm the flag 10 s ago (< 30 s) → the next gain is the identify echo, swallowed.
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
        clear.assert_not_awaited()  # swallowed — feed-fault clear NOT run
        assert client.ams_refresh_tray.call_count == 0

    async def test_genuine_reinsert_over_30s_runs_clear(self, db_session, monkeypatch):
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        # Arm the flag 31 s ago (> 30 s) → GC'd; the gain is a genuine reseat and clears.
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic() - 31
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # genuine gain
        clear.assert_awaited_once()
        client.ams_refresh_tray.assert_called_once_with(0, 0)


class TestPhysicalCycleNote:
    """A genuine presence GAIN whose preceding absence lasted >= _MIN_PHYSICAL_ABSENT_S
    records a physical roll swap via spool_tagless.note_physical_cycle (the W1 latch
    release / W5 prompt). A sub-second flap, an echo, a drying flap, and the first-push
    seed all suppress it."""

    @pytest.fixture(autouse=True)
    def _spy_note(self, monkeypatch):
        from backend.app.services import spool_tagless

        note = AsyncMock()
        monkeypatch.setattr(spool_tagless, "note_physical_cycle", note)
        return note

    async def test_qualified_absence_fires_once(self, db_session, monkeypatch, _spy_note):
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # first push seeds present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss -> stamps absence
        # Backdate the absence past the physical-swap threshold.
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
        _spy_note.assert_awaited_once_with(1, 0, 0)

    async def test_short_flap_does_not_fire(self, db_session, monkeypatch, _spy_note):
        # 16 ms flap (a runout-instant state flap) -> absence < 5 s -> no cycle.
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss (stamp now)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain, ~0 s later
        _spy_note.assert_not_awaited()

    async def test_first_push_seed_never_fires(self, db_session, monkeypatch, _spy_note):
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # first push, present
        _spy_note.assert_not_awaited()

    async def test_echo_gain_does_not_fire(self, db_session, monkeypatch, _spy_note):
        # An identify-flap echo gain is swallowed before the cycle note (an echo is not
        # a physical event) even though the backdated absence would otherwise qualify.
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic()  # arm the echo flag
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
        _spy_note.assert_not_awaited()

    async def test_drying_gain_does_not_fire(self, db_session, monkeypatch, _spy_note):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain while drying
        _spy_note.assert_not_awaited()


class TestEngagedFilamentDefer:
    """A commanded ams_get_rfid is refused by the client while any filament is loaded
    (``tray_now != 255``); the client's WARNING names the ENGAGED slot, so two eligible
    tagged slots swept while one is engaged log two IDENTICAL warnings in the same
    instant (the live 07-20 double log). The need-driven paths pre-check the same
    predicate and defer QUIETLY (one DEBUG, no WARNING), stamping nothing so the slot's
    eligibility is untouched and the NEXT terminal retries once filament is unloaded."""

    def _client(self):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        return client

    async def _bind_tagged(self, db_session, tray_id, tag):
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=tag)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=tray_id))
        await db_session.commit()

    async def test_engaged_terminal_defers_then_retries_when_unloaded(self, db_session, sessions, monkeypatch):
        # rfid_refresh slot: engaged filament ⇒ the sweep commands NOTHING; once the
        # filament is unloaded (tray_now=255) the next terminal sends exactly one.
        await self._bind_tagged(db_session, 0, _VALID_TAG)
        client = self._client()
        status = _pstate([_stale_remain_tray(0)], gcode_state="FINISH", subtask_id="t1", tray_now=1)  # slot 1 engaged
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()  # engaged → deferred, no ams_get_rfid

        status.tray_now = 255  # filament unloaded
        status.subtask_id = "t2"  # a NEW print reached terminal
        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)  # deferred refresh now runs

    async def test_engaged_preserves_discovery_eligibility(self, db_session, sessions, monkeypatch):
        # A DISCOVERY read deferred for engaged filament must NOT consume the unanswered
        # cycle — the next (unloaded) terminal still sees it and reads once.
        _arm_cycle(1, 0, 0)
        client = self._client()
        status = _pstate([_tray(0, state=11)], gcode_state="FINISH", subtask_id="t1", tray_now=2)  # engaged
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()
        assert ams_presence._unanswered_cycle(1, 0, 0) is True  # discovery evidence preserved

        status.tray_now = 255
        status.subtask_id = "t2"
        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_two_engaged_tagged_slots_emit_no_warning(self, db_session, sessions, monkeypatch, caplog):
        # THE double-log pin: two tagged slots eligible while slot 1 is engaged. The old
        # path logged the SAME "filament loaded from AMS 1 slot 2" WARNING once per slot
        # (the message names the engaged slot, not the target). The pre-check now emits
        # zero client commands and zero WARNINGs — one quiet DEBUG per deferred slot.
        await self._bind_tagged(db_session, 0, _VALID_TAG)
        await self._bind_tagged(db_session, 2, "FEDCBA0987654321")
        client = self._client()
        status = _pstate(
            [_stale_remain_tray(0), _tray(1, state=11), _stale_remain_tray(2)],
            gcode_state="FINISH",
            subtask_id="t1",
            tray_now=1,  # slot 1 engaged — blocks the whole AMS
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        with caplog.at_level(logging.DEBUG, logger="backend.app.services.ams_presence"):
            await ams_presence.on_printer_terminal(1)

        client.ams_refresh_tray.assert_not_called()  # both eligible slots deferred
        ap = [r for r in caplog.records if r.name == "backend.app.services.ams_presence"]
        assert not [r for r in ap if r.levelno >= logging.WARNING]  # no (doubled) WARNING
        deferred = [r for r in ap if "filament engaged" in r.getMessage()]
        assert len(deferred) == 2 and all(r.levelno == logging.DEBUG for r in deferred)  # one DEBUG per slot

    async def test_disengaged_sends_exactly_once_per_slot(self, db_session, sessions, monkeypatch):
        # Control: unloaded (tray_now=255) ⇒ each eligible tagged slot is read exactly
        # once — no double-invocation.
        await self._bind_tagged(db_session, 0, _VALID_TAG)
        client = self._client()
        status = _pstate([_stale_remain_tray(0)], gcode_state="FINISH", subtask_id="t1", tray_now=255)
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_command_identify_defer_logs_debug_and_stamps_nothing(self, monkeypatch, caplog):
        # The defer path directly: DEBUG (not WARNING), no client command, and NONE of
        # the read bookkeeping (echo arm / identity-learned / discovery stamp) mutated —
        # so eligibility is genuinely preserved.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], tray_now=1), client=client)

        with caplog.at_level(logging.DEBUG, logger="backend.app.services.ams_presence"):
            ok, msg = await ams_presence.command_identify(1, 0, 0, source="terminal_sweep", reason="rfid_refresh")

        assert ok is False and msg == "filament engaged"
        client.ams_refresh_tray.assert_not_called()
        ap = [r for r in caplog.records if r.name == "backend.app.services.ams_presence"]
        assert any(r.levelno == logging.DEBUG and "deferred" in r.getMessage() for r in ap)
        assert not [r for r in ap if r.levelno >= logging.WARNING]
        assert ams_presence._echo_pending == {}
        assert ams_presence._slot_read_at == {}
        assert ams_presence._discovery_read_at == {}

    async def test_manual_refresh_not_preempted_by_engaged(self, monkeypatch):
        # Operator bypass (enforce_need=False) is wire-safety-only: the engaged pre-check
        # does NOT apply, so the command still reaches the client, which returns its own
        # verbatim refusal. Explicit intent, explicit answer — never a silent skip.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (
            False,
            "Please unload filament first. Currently loaded: AMS 1 slot 2",
        )
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], tray_now=1), client=client)

        ok, msg = await ams_presence.command_identify(1, 0, 0, source="manual_refresh", enforce_need=False)
        assert ok is False and "unload filament" in msg
        client.ams_refresh_tray.assert_called_once_with(0, 0)  # reached the client, not pre-empted

    def test_engaged_helper_mirrors_the_client_sentinel(self, monkeypatch):
        # _filament_engaged reads the live PrinterState.tray_now (get_status) against the
        # single-origin 255 sentinel; None / missing / 255 all read as not-engaged.
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(tray_now=255))
        assert ams_presence._filament_engaged(1) is False
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(tray_now=1))
        assert ams_presence._filament_engaged(1) is True
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(tray_now=254))
        assert ams_presence._filament_engaged(1) is True  # external spool engaged
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace())
        assert ams_presence._filament_engaged(1) is False  # missing attr → unloaded default
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: None)
        assert ams_presence._filament_engaged(1) is False  # printer gone → not engaged


class TestLoadedAtReStamp:
    """A QUALIFIED genuine presence GAIN adjudicates the currently-bound row via
    spool_binding.stamp_loaded_for_slot — the re-stampable FIFO ordinal (006-H2S).
    ``qualified`` is the WIDER gate than note_physical_cycle's ``physical_cycle``: a
    MEASURED >= 5 s absence OR an unknown-duration one (boot-spanning / coalesced edges)
    both fire it, honouring rule 2's restart-durability contract. A MEASURED sub-5 s flap,
    an echo, a drying flap, and the first-push seed all suppress it."""

    @pytest.fixture(autouse=True)
    def _spy_stamp(self, monkeypatch):
        from backend.app.services import spool_binding, spool_tagless

        stamp = AsyncMock(return_value=True)
        monkeypatch.setattr(spool_binding, "stamp_loaded_for_slot", stamp)
        # Keep note_physical_cycle inert so the physical_cycle block doesn't open a session.
        monkeypatch.setattr(spool_tagless, "note_physical_cycle", AsyncMock())
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        return stamp

    async def test_qualified_absence_fires_once(self, db_session, monkeypatch, _spy_stamp):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
        _spy_stamp.assert_awaited_once_with(db_session, 1, 0, 0)

    async def test_gain_after_boot_absent_seed_invokes_adjudicator(self, db_session, monkeypatch, _spy_stamp):
        # Server restarted with the slot EMPTY (the first push seeds it absent, leaving NO
        # _absent_since entry), then a roll is inserted later. absent_for is None → UNKNOWN
        # duration → qualified True but physical_cycle False. The FIFO re-stamp must still
        # adjudicate (rule 2's restart-durability contract); the mint/prompt latch does not.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # boot seed: absent
        assert (1, 0, 0) not in ams_presence._absent_since  # no absence start ever observed
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # later insert → gain
        _spy_stamp.assert_awaited_once_with(db_session, 1, 0, 0)

    async def test_short_flap_does_not_fire(self, db_session, monkeypatch, _spy_stamp):
        # A sub-_MIN_PHYSICAL_ABSENT_S flap is a runout-instant firmware state flap, not a
        # physical re-seat — the 5 s wire-flap debounce holds the adjudicator.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss (stamp now)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain ~0 s later
        _spy_stamp.assert_not_awaited()

    async def test_first_push_seed_never_fires(self, db_session, monkeypatch, _spy_stamp):
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # first push
        _spy_stamp.assert_not_awaited()

    async def test_echo_gain_does_not_fire(self, db_session, monkeypatch, _spy_stamp):
        # An identify-flap echo gain is swallowed before the physical_cycle block even
        # though the backdated absence would otherwise qualify.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic()  # arm the echo flag
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
        _spy_stamp.assert_not_awaited()

    async def test_drying_gain_does_not_fire(self, db_session, monkeypatch, _spy_stamp):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain while drying
        _spy_stamp.assert_not_awaited()


class TestIdleGainNeedSurvivesDefer:
    """R4: a deferred / suppressed idle-gain discovery need is never LOST. command_identify
    stamps note_identity_learned ONLY on a successful wire command, so every defer
    (engaged filament, 30 s identify-gate refusal) leaves the unanswered physical cycle
    intact — identify_needed still returns 'discovery', which the terminal sweep re-fires.
    A successful read, by contrast, answers the cycle and the untouched tagless slot is
    never re-read again (wire-safety doctrine)."""

    async def test_engaged_filament_defer_preserves_discovery_need(self, db_session, monkeypatch):
        _arm_cycle(1, 0, 0)  # an unanswered qualified physical cycle (a >=5 s reseat)
        tray = _tray(0, state=11)  # present, untagged
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        # tray_now=0 → filament engaged → command_identify defers before the wire.
        _patch_pm(monkeypatch, status=_pstate([tray], gcode_state="IDLE", tray_now=0), client=client)

        ok, msg = await ams_presence.command_identify(1, 0, 0, source="idle_gain", reason="discovery")
        assert ok is False and msg == "filament engaged"
        client.ams_refresh_tray.assert_not_called()  # never reached the wire, stamped nothing
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, tray, spoolman_active=False)
        assert reason == "discovery"  # need survives → terminal sweep will catch it

    async def test_gate_refusal_defer_preserves_discovery_need(self, db_session, monkeypatch):
        _arm_cycle(1, 0, 0)
        tray = _tray(0, state=11)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (False, "identify gate active")  # 30 s gate refused
        _patch_pm(monkeypatch, status=_pstate([tray], gcode_state="IDLE"), client=client)

        ok, _msg = await ams_presence.command_identify(1, 0, 0, source="idle_gain", reason="discovery")
        assert ok is False
        client.ams_refresh_tray.assert_called_once_with(0, 0)  # tried the wire, was refused
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, tray, spoolman_active=False)
        assert reason == "discovery"  # refused command stamps no identity-learned

    async def test_successful_read_answers_the_cycle(self, db_session, monkeypatch):
        _arm_cycle(1, 0, 0)
        tray = _tray(0, state=11)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([tray], gcode_state="IDLE"), client=client)

        ok, _msg = await ams_presence.command_identify(1, 0, 0, source="idle_gain", reason="discovery")
        assert ok is True
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, tray, spoolman_active=False)
        assert reason is None  # cycle answered; an untouched tagless slot is never re-read


class TestIdentifyFlapNotAQualifiedCycle:
    """Incident 2026-07-21 (printer 5, unattended): overnight RFID re-reads on
    tagless slots flapped the tray ABSENT→PRESENT for ~10–20 s, and the ≥5 s gain
    qualifier banked each flap as a QUALIFIED physical cycle — which
    ``spool_respool._swap_evidence`` reads as "somebody physically cycled a roll",
    so a Tier-3 respool prompt woke the operator over a roll nobody touched.

    Invariant: an absence an identify explains must NEVER produce a QUALIFIED physical
    cycle (``last_physical_cycle_age`` stays None), however long it ran. The ≥5 s
    filter still measures duration only — identity is a separate, ANDed gate. A real
    human swap with NO identify activity anywhere is untouched, and the pre-existing
    echo-swallow / sub-5 s-flap guards are unchanged.
    """

    @pytest.fixture(autouse=True)
    def _inert_gain_consumers(self, monkeypatch):
        # The GAIN edge fires clear_on_reinsert unconditionally and (when the tiers
        # open) note_physical_cycle / stamp_loaded_for_slot. Keep all three inert so a
        # case asserts purely on whether a QUALIFIED cycle was RECORDED, not on the DB.
        from backend.app.services import spool_binding, spool_tagless

        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        monkeypatch.setattr(spool_tagless, "note_physical_cycle", AsyncMock())
        monkeypatch.setattr(spool_binding, "stamp_loaded_for_slot", AsyncMock(return_value=True))

    async def test_state9_commanded_identify_flap_is_not_a_qualified_cycle(self, db_session, monkeypatch):
        # THE incident pin (fails on pre-fix code — verified by mutation). A commanded
        # identify on a SEATED-yet-unread (state 9) slot: record_reread never arms the
        # echo there, so the old echo lane cannot see the flap — the leak. The AMS is
        # IDENTIFYING while the tray is unloaded, which is what now disqualifies the gain.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate(
            [_tray(0, state=9)],
            gcode_state="RUNNING",  # RUNNING → the idle-gain re-read lane stays out of it
            ams_status_main=ams_presence.AMS_STATUS_IDENTIFYING,
        )
        _patch_pm(monkeypatch, status=status, client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present

        # Commanded identify while the tray reports state 9 → NO echo armed (the leak).
        ok, _ = await ams_presence.command_identify(1, 0, 0, source="terminal_sweep", reason="rfid_refresh")
        assert ok is True
        assert ams_presence._echo_pending == {}  # state-9 slot: echo lane blind to this flap

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # identify unloads
        assert ams_presence._absent_under_identify[(1, 0, 0)] is True  # flagged at the absence start
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )  # ≥5 s absence — the exact duration the old qualifier trusted
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # settle-back gain

        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None  # NO qualified cycle recorded
        assert ams_presence.recent_gain_age(1, 0, 0) is not None  # the non-qualified gain stamp still updates

    async def test_firmware_autonomous_read_flap_is_not_a_qualified_cycle(self, db_session, monkeypatch):
        # No command_identify at all — a firmware-AUTONOMOUS re-read. It leaves no echo
        # and no command, so ONLY the unit-scoped ams_status_main == IDENTIFYING signal
        # observed during the absence can disqualify it. It must.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate(
            [_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=ams_presence.AMS_STATUS_IDENTIFYING
        )
        _patch_pm(monkeypatch, status=status, client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        assert ams_presence._echo_pending == {}  # nobody commanded anything
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # autonomous unload
        assert ams_presence._absent_under_identify[(1, 0, 0)] is True
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # settle-back gain

        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None
        client.ams_refresh_tray.assert_not_called()  # firmware did the read; the farm commanded nothing

    async def test_real_human_swap_with_no_identify_is_qualified(self, db_session, monkeypatch):
        # The other side of the gate: a ≥5 s absence with NO identify activity anywhere
        # (ams idle, no echo, no command) is a genuine roll swap and DOES record a
        # qualified cycle — the evidence _swap_evidence needs to prompt a real refill.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0)  # ams idle throughout
        _patch_pm(monkeypatch, status=status, client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # human pulls it
        assert ams_presence._absent_under_identify[(1, 0, 0)] is False  # no identify to explain it
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # reseated

        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0  # qualified cycle recorded

    async def test_echo_armed_present_slot_identify_unchanged(self, db_session, monkeypatch):
        # Regression guard: a commanded identify on a PRESENT slot arms the echo, and
        # the settle-back gain is swallowed BEFORE the qualifier — exactly as today. No
        # qualified cycle either way; behaviour is identical pre/post fix.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(
            monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE", ams_status_main=0), client=client
        )

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        ams_presence.record_reread(1, 0, 0)  # present slot → echo armed
        assert (1, 0, 0) in ams_presence._echo_pending
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # identify flap loss
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain swallowed

        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None  # swallowed → never a cycle

    async def test_sub_5s_flap_still_unqualified(self, db_session, monkeypatch):
        # Regression guard: a MEASURED sub-5 s flap (a runout-instant firmware state
        # flap, no identify) is unqualified by the duration filter, unchanged by this fix.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(
            monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0), client=client
        )

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss (stamp now)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain ~0 s later

        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None


class TestPromotedState10DrivesDiscovery:
    """003-H2S: apply_tray_exist_bits now promotes a stuck mid-print-insert slot
    9→10. That merged state-10 tray must flow the presence pipeline — _tray_present
    True, and with a qualified physical cycle recorded but unanswered, identify_needed
    returns 'discovery' (one read whose expected tagless failure is suppressed
    farm-side). An untouched state-10 slot with NO unanswered cycle stays None, so
    the discovery is driven by the change, not by presence alone."""

    async def test_state10_untagged_with_unanswered_cycle_needs_discovery(self, db_session):
        tray = _tray(0, state=10)  # promoted "present, not fed", still unconfigured/untagged
        assert ams_presence._tray_present(tray) is True
        _arm_cycle(1, 0, 0)  # a qualified physical cycle recorded, not yet answered
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, tray, spoolman_active=False)
        assert reason == "discovery"

    async def test_state10_untagged_without_cycle_is_none(self, db_session):
        # Control: presence alone is not a discovery trigger — an untouched tagless
        # slot must never be read (the 0700_0081 factory the need-check closes).
        tray = _tray(0, state=10)
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, tray, spoolman_active=False)
        assert reason is None


class TestOwedIdentityUnansweredAccessor:
    """``identity_unanswered`` is the public, non-consuming view of the discovery
    evidence that spool_tagless's config-settle gate reads — the fork's one answer to
    "does anybody know what is in this slot?"."""

    def test_mirrors_the_private_evidence_test(self):
        assert ams_presence.identity_unanswered(1, 0, 0) is False
        _arm_cycle(1, 0, 0)
        assert ams_presence.identity_unanswered(1, 0, 0) is True
        ams_presence.note_identity_learned(1, 0, 0)  # a read answered it
        assert ams_presence.identity_unanswered(1, 0, 0) is False

    def test_asking_does_not_consume_the_evidence(self):
        _arm_cycle(1, 0, 0)
        for _ in range(3):
            assert ams_presence.identity_unanswered(1, 0, 0) is True


class TestOwedReadObservability:
    """2026-07-25: the owed discovery read deferred QUIETLY (one DEBUG) for six hours.
    A defer that lasts past _OWED_READ_WARN_AFTER_S is no longer pacing — it is a
    standing unknown identity, and it says so once an hour."""

    def _client(self, *, refusal=None):
        client = MagicMock()
        client.ams_write_refusal.return_value = refusal
        client.ams_unit_drying.return_value = False
        client.ams_refresh_tray.return_value = (True, "ok")
        return client

    async def _run(self, db_session, monkeypatch, *, cycle_age, tray_now=1, gcode_state="IDLE"):
        client = self._client()
        state = _pstate([_tray(0, state=11)], gcode_state=gcode_state, tray_now=tray_now)
        _patch_pm(monkeypatch, status=state, client=client)
        _arm_cycle(1, 0, 0, age=cycle_age)
        return await ams_presence.maybe_command_owed_identify(db_session, 1, 0, 0, _tray(0, state=11), state), client

    async def test_warns_once_then_suppresses_within_the_rewarn_window(self, db_session, monkeypatch, caplog):
        with caplog.at_level(logging.DEBUG, logger="backend.app.services.ams_presence"):
            ok, client = await self._run(db_session, monkeypatch, cycle_age=ams_presence._OWED_READ_WARN_AFTER_S + 60)
            assert ok is False
            client.ams_refresh_tray.assert_not_called()
            warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
            assert len(warnings) == 1
            assert "identity unknown" in warnings[0].getMessage()
            assert "filament engaged" in warnings[0].getMessage()

            # Same slot again inside the re-warn window → still deferred, but silent.
            await ams_presence.maybe_command_owed_identify(
                db_session, 1, 0, 0, _tray(0, state=11), ams_presence.printer_manager.get_status(1)
            )
            assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    async def test_recent_cycle_defers_without_warning(self, db_session, monkeypatch, caplog):
        with caplog.at_level(logging.DEBUG, logger="backend.app.services.ams_presence"):
            ok, client = await self._run(db_session, monkeypatch, cycle_age=30.0)
        assert ok is False
        client.ams_refresh_tray.assert_not_called()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]  # ordinary pacing

    async def test_disengaged_idle_slot_is_read_not_warned(self, db_session, monkeypatch, caplog):
        with caplog.at_level(logging.DEBUG, logger="backend.app.services.ams_presence"):
            ok, client = await self._run(
                db_session, monkeypatch, cycle_age=ams_presence._OWED_READ_WARN_AFTER_S + 60, tray_now=255
            )
        assert ok is True
        client.ams_refresh_tray.assert_called_once_with(0, 0)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert ams_presence._unanswered_cycle(1, 0, 0) is False  # the read answered it


class TestReadOccasionPacing:
    """2026-08-07 identify storm: 1000 commanded reads on one printer in a day (367/368
    on two more), one every ~31 s — the client's per-printer identify gate being the only
    thing pacing them. Cause: a STANDING condition (a DB binding whose tagged spool the
    tray does not show) re-derived the same ``rfid_refresh`` verdict on every reconcile /
    pipeline pass. Because each command re-armed the 30 s gate and the drain re-commanded
    the instant it cleared, the gate's duty cycle was ~100 % and the slot pipeline's
    ``identify_in_flight`` row deferred EVERY slot decision on those printers.

    The fix is an OCCASION: standing evidence buys ONE read, and only a new CAUSE (a
    qualified physical cycle, or the terminal's between-prints policy) buys another.
    Doctrine rule 6/7 — by cause, never by a timer laid over the verdict.
    """

    async def _bind(self, db_session, *, tag=_VALID_TAG, spent=None, tray_id=0, **kw):
        spool = Spool(
            material="PETG", data_origin="rfid_auto" if tag else "ams_auto", tag_uid=tag, spent_at=spent, **kw
        )
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=tray_id))
        await db_session.commit()
        return spool

    async def test_storm_shape_state9_bound_tagged_owes_nothing(self, db_session):
        # The live printer-6/10 shape: a stale tagged binding on a tray that is NOT
        # seated. A read here fails exactly like a tagless one and raises a
        # never-clearing 0700_0081 — this is a RELEASE problem (doctrine rule 9), never
        # a read reason. True with the occasion open OR closed: presence is the hard gate.
        await self._bind(db_session)
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=9), False) is None
        _arm_occasion()
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=9), False) is None

    async def test_h2c_state0_bound_shape_owes_nothing(self, db_session):
        # H2C idle empties report state 0 — an unknown dialect code, i.e. presence
        # UNKNOWN. Unknown is not evidence of a seated spool, so it is never a read
        # reason, occasion or no occasion.
        await self._bind(db_session)
        _arm_occasion()
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=0), False) is None

    async def test_two_passes_over_an_unchanged_slot_command_at_most_one_read(self, db_session, monkeypatch):
        # THE pacing pin. Two consecutive passes over a slot nothing happened to: the
        # first spends the occasion, the second owes nothing at all.
        await self._bind(db_session)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        _arm_occasion()  # e.g. a terminal's between-prints policy opened one

        tray = _tray(0, state=11)
        for _ in range(2):
            reason = await ams_presence.identify_needed(db_session, 1, 0, 0, tray, False)
            if reason is not None:
                await ams_presence.command_identify(1, 0, 0, source="reconcile", reason=reason)

        assert client.ams_refresh_tray.call_count == 1
        assert ams_presence._read_occasion_open(1, 0, 0) is False  # consumed by the read

    async def test_a_new_qualified_cycle_reopens_the_occasion(self, db_session, monkeypatch):
        # The gate narrows the OCCASION, never the capability: somebody moving a roll
        # buys the slot another answer.
        await self._bind(db_session)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        _arm_occasion()
        await ams_presence.command_identify(1, 0, 0, source="reconcile", reason="rfid_refresh")
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=11), False) is None

        ams_presence._note_gain(1, 0, 0, qualified=True)  # a real pull+reseat
        ams_presence.note_identity_learned(1, 0, 0)  # …whose discovery the firmware answered
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=11), False) == "rfid_refresh"

    async def test_refusal_defers_without_waiting_and_without_burning_the_occasion(self, db_session, monkeypatch):
        # Invariant 2: a caller may DEFER on a refused wire, never pre-approve — and
        # never WAIT for the gate, because the gate it would wait on is armed by our own
        # previous identify (that self-clocked loop IS the storm's cadence).
        client = MagicMock()
        client.ams_write_refusal.return_value = "identify_in_flight"
        client.ams_unit_drying.return_value = False
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        state = _pstate([_tray(0, state=11)], gcode_state="IDLE", tray_now=255)
        _patch_pm(monkeypatch, status=state, client=client)
        _arm_cycle(1, 0, 0)
        _arm_occasion()

        ok = await ams_presence.maybe_command_owed_identify(db_session, 1, 0, 0, _tray(0, state=11), state)

        assert ok is False
        client.ams_refresh_tray.assert_not_called()
        client.wait_ams_settle.assert_not_awaited()  # deferred, never awaited the gate
        assert ams_presence._unanswered_cycle(1, 0, 0) is True  # evidence untouched
        assert ams_presence._read_occasion_open(1, 0, 0) is True  # occasion untouched

    async def test_spent_occupied_state10_owes_one_discovery(self, db_session, monkeypatch):
        # A spent-latched binding under a SEATED roll the wire cannot identify: the roll
        # in the slot is a newcomer nobody has named. ``discovery`` (not rfid_refresh) is
        # what suppresses its expected no-tag failure farm-side.
        await self._bind(db_session, spent=datetime.utcnow())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=10)]), client=client)
        _arm_occasion()

        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) == "discovery"
        await ams_presence.command_identify(1, 0, 0, source="terminal_sweep", reason="discovery")
        # Second pass over the SAME occupancy epoch: nothing more is owed. Every failed
        # read leaves a printer-side 0700_0081 that only a power-cycle clears (rule 5).
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) is None

        ams_presence._note_gain(1, 0, 0, qualified=True)  # a NEW roll goes in
        ams_presence.note_identity_learned(1, 0, 0)  # cycle answered; the spent latch is not
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) == "discovery"

    async def test_spent_occupied_state11_earns_its_read(self, db_session):
        """State 11 is the same roll threaded on to the hub, so the read must WAIT for the
        idle edge — but waiting is wire safety, and wire safety is enforced in
        ``command_identify``, which defers on engaged filament and spends nothing.

        Refusing the VERDICT here instead is what created the gap: ``slot_state`` row 5
        emits ``spent_occupied_owed_identify`` on ``present is True`` (10 or 11), so a
        state-11 spent-occupied slot asked for a read the need authority could never
        grant — a request standing forever, resolving nothing."""
        await self._bind(db_session, tag=None, spent=datetime.utcnow())
        _arm_occasion()
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=11), False) == "discovery"

    async def test_spent_occupied_with_a_live_tag_is_not_discovery(self, db_session):
        # The wire named the roll: there is nothing to discover, and the read (if the
        # occasion is open) is an answerable refresh, never an expected failure.
        await self._bind(db_session, spent=datetime.utcnow())
        _arm_occasion()
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10, tag=_VALID_TAG), False)
        assert reason == "rfid_refresh"

    async def test_spent_occupied_without_an_occasion_owes_nothing(self, db_session):
        await self._bind(db_session, spent=datetime.utcnow())
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) is None


class TestProdShapeReplayThroughTheRealPredicate:
    """c93ea73f's contract, replayed against the REAL predicate (the pipeline-side pin
    lives in test_slot_pipeline.py and must stay green untouched).

    008-H2C AMS2 slot2, 2026-08-02: a dialect-odd slot (state 9, no filament type, no
    tag, stuck exist bit) resolved ``NONE(identity_unresolved)`` on EVERY push — a
    standing condition, not an event — and the pipeline answered each one with a
    discovery identify. ``identity_unresolved`` is a REQUEST this predicate must clear;
    it clears it with None, because state 9 is not presence and no qualified physical
    cycle was ever recorded.
    """

    PROD_TRAY = {"id": 2, "state": 9, "tray_type": ""}

    async def test_three_passes_zero_reads(self, db_session):
        for _ in range(3):
            assert await ams_presence.identify_needed(db_session, 1, 2, 2, dict(self.PROD_TRAY), False) is None

    async def test_three_passes_zero_reads_with_a_spent_binding(self, db_session):
        # Same shape, now with a spent-latched binding on the slot — the newest arm.
        # It requires a SEATED (state 10) tray, so the dialect-odd state-9 slot still
        # buys nothing, occasion open or not.
        spool = Spool(material="PETG", data_origin="ams_auto", spent_at=datetime.utcnow())
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=2, tray_id=2))
        await db_session.commit()
        _arm_occasion(1, 2, 2)

        for _ in range(3):
            assert await ams_presence.identify_needed(db_session, 1, 2, 2, dict(self.PROD_TRAY), False) is None


class TestCauseBasedEdgeSuppressionWidened:
    """Phantom qualified cycles (2026-08-07). Gain/absence edges fabricated by
    NON-PHYSICAL causes were arming real actions — fresh-roll prompts for slots nobody
    touched, never-fed ``loaded_at`` re-stamps. The 2026-07-21 wave disqualified
    identify-explained edges but left three leaks: a read commanded on a state-9 tray
    (``record_reread`` never arms there), a firmware-autonomous read whose IDENTIFYING
    flag rises and falls between edges, and the print-start AMS engage transient.

    Doctrine rule 6 / invariant 6: suppression is BY CAUSE — never a timer or damper on
    gains. Presence itself still updates on a suppressed edge, and the recovery lanes
    (``clear_on_reinsert`` / refill auto-resume) still fire: only the ACTION tiers that
    assume a human moved a roll are withheld.
    """

    @pytest.fixture(autouse=True)
    def _inert_gain_consumers(self, monkeypatch):
        from backend.app.services import spool_binding, spool_tagless

        self.clear = AsyncMock()
        self.note_cycle = AsyncMock()
        self.restamp = AsyncMock(return_value=True)
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", self.clear)
        monkeypatch.setattr(spool_tagless, "note_physical_cycle", self.note_cycle)
        monkeypatch.setattr(spool_binding, "stamp_loaded_for_slot", self.restamp)

    async def _cycle(self, db_session, *, mid_absence=None):
        """seed present → loss → (optional mid-absence push) → backdated ≥5 s → gain."""
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        if mid_absence is not None:
            mid_absence()
            await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)

    async def test_state9_commanded_read_edge_is_not_a_qualified_cycle(self, db_session, monkeypatch):
        # LEAK (b): a read commanded while the tray reports state 9 arms no echo, and the
        # AMS never reports IDENTIFYING on a push we see — so the 07-21 wave's two arms
        # are both blind and the ~15 s flap banked as a human roll swap.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        ok, _ = await ams_presence.command_identify(1, 0, 0, source="terminal_sweep", reason="rfid_refresh")
        assert ok is True and ams_presence._echo_pending == {}  # the echo lane is blind here

        await _push(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        assert ams_presence._absent_under_identify[(1, 0, 0)] is False  # …and so is the loss-edge flag
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)

        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None  # no qualified cycle
        self.note_cycle.assert_not_awaited()  # no fresh-roll prompt
        self.restamp.assert_not_awaited()  # no never-fed loaded_at re-stamp
        self.clear.assert_awaited()  # …but the feed-fault clear still runs (presence is truth)

    async def test_a_command_explains_only_one_flap(self, db_session, monkeypatch):
        # The other side of leak (b): the cause is consumed by the first gain after it, so
        # a genuine pull+reseat made AFTER the identify settled is still QUALIFIED. A
        # lingering "we read this slot N s ago" stamp would swallow it — the failure mode
        # _identify_explains_absence's docstring names.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        await _push(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        await ams_presence.command_identify(1, 0, 0, source="terminal_sweep", reason="rfid_refresh")
        await self._cycle(db_session)  # the identify's own flap — suppressed
        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None

        await self._cycle(db_session)  # a human pull+reseat afterwards
        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0
        self.note_cycle.assert_awaited_once()

    async def test_identifying_observed_mid_absence_is_not_a_qualified_cycle(self, db_session, monkeypatch):
        # LEAK (c): a firmware-AUTONOMOUS read. No command, and the IDENTIFYING flag is
        # down again by the time the gain lands — only a SAMPLED observation from inside
        # the absence window can explain it.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        def _flag_identifying():
            status.ams_status_main = ams_presence.AMS_STATUS_IDENTIFYING

        await self._cycle(db_session, mid_absence=_flag_identifying)
        assert ams_presence._identifying_seen_at.get(1) is not None
        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None
        self.note_cycle.assert_not_awaited()
        client.ams_refresh_tray.assert_not_called()  # the farm commanded nothing

    async def test_print_start_transient_is_not_a_qualified_cycle(self, db_session, monkeypatch):
        # LEAK (d): starting a print engages the AMS; trays disengage and re-seat with
        # nobody having touched a roll (2026-08-07 15:12:03, three printers).
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        ams_presence.note_running_edge(1)
        await self._cycle(db_session)

        assert ams_presence.last_physical_cycle_age(1, 0, 0) is None
        self.note_cycle.assert_not_awaited()
        self.restamp.assert_not_awaited()
        self.clear.assert_awaited()  # jam recovery's re-insert clear is NOT withheld
        assert ams_presence.recent_gain_age(1, 0, 0) is not None  # presence maps still update

    async def test_a_stale_running_edge_suppresses_nothing(self, db_session, monkeypatch):
        # The window is a CAUSE window, not a damper: past it the edge is judged exactly
        # as it was before.
        client = MagicMock()
        status = _pstate([_tray(0, state=9)], gcode_state="RUNNING", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        ams_presence._running_edge_at[1] = ams_presence.time.monotonic() - (ams_presence._RUNNING_EDGE_TRANSIENT_S + 5)
        await self._cycle(db_session)
        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0
        self.note_cycle.assert_awaited_once()

    async def test_pause_window_gain_stays_fully_qualified(self, db_session, monkeypatch):
        # NON-NEGOTIABLE: a gain while the printer sits in PAUSE is the same-slot runout
        # refill / jam reinsert that spool_recovery.maybe_auto_resume_on_refill and
        # clear_on_reinsert are waiting for. Even with a fresh RUNNING edge on the books,
        # the print-start arm must not touch it.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="PAUSE", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        ams_presence.note_running_edge(1)
        await self._cycle(db_session)

        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0  # fully qualified
        self.note_cycle.assert_awaited_once()
        self.restamp.assert_awaited()
        self.clear.assert_awaited()

    async def test_real_pull_and_reinsert_while_idle_is_still_qualified(self, db_session, monkeypatch):
        # The control: no command, no IDENTIFYING, no print start → a genuine roll swap.
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        status = _pstate([_tray(0, state=9)], gcode_state="IDLE", ams_status_main=0)
        _patch_pm(monkeypatch, status=status, client=client)

        await self._cycle(db_session)

        assert ams_presence.last_physical_cycle_age(1, 0, 0) < 1.0
        self.note_cycle.assert_awaited_once()
        self.restamp.assert_awaited()


class TestTerminalRemainRefresh:
    """D4 (2026-08-07): bound TAGGED trays stuck at wire ``remain = -1`` — the full RFID
    data read never completed — were never re-read, so
    ``usage_tracker.maybe_reconcile_tagged_ledger_decrease`` (the W6 auto-repair) had no
    wire truth and over-label ledgers persisted (live: a spool at 1899.9 g used against a
    1000 g label). Doctrine rule 5 allows need-driven reads on RFID-bound slots; rule 8
    makes wire remain% the truth for a tagged row.

    The need is derived at the TERMINAL only — ``reconcile_slot_config``'s charter is
    occasions, never new permission (``test_tagged_slot_gets_no_refresh_from_this_lane``
    pins that lane from the other side).
    """

    async def _bound(self, db_session, **kw):
        fields = {"material": "PETG", "data_origin": "rfid_auto", "tag_uid": _VALID_TAG, "label_weight": 1000}
        fields.update(kw)
        spool = Spool(**fields)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        return spool

    async def _cause(self, db_session, tray):
        return await ams_presence._terminal_read_occasion(db_session, 1, 0, 0, tray)

    async def test_wire_remain_missing_owes_a_refresh(self, db_session):
        await self._bound(db_session)
        assert await self._cause(db_session, _tray(0, state=10, remain=-1)) == "wire_remain_missing"
        assert await self._cause(db_session, _tray(0, state=10, remain=None)) == "wire_remain_missing"
        assert await self._cause(db_session, _tray(0, state=10, remain="n/a")) == "wire_remain_missing"

    async def test_ledger_past_label_owes_a_refresh(self, db_session):
        await self._bound(db_session, weight_used=1899.9)
        assert await self._cause(db_session, _tray(0, state=10, remain=42)) == "ledger_over_label"

    async def test_healthy_tagged_slot_owes_nothing(self, db_session):
        # The de-amplification: remain arrives on every ordinary AMS push for a tagged
        # tray, so a healthy one needs no commanded read at all.
        await self._bound(db_session, weight_used=250.0)
        assert await self._cause(db_session, _tray(0, state=10, remain=75)) is None

    async def test_weight_locked_owes_nothing(self, db_session):
        # The operator owns this row's weight; a wire refresh must not fight it.
        await self._bound(db_session, weight_used=1899.9, weight_locked=True)
        assert await self._cause(db_session, _tray(0, state=10, remain=-1)) is None

    async def test_state11_owes_nothing(self, db_session):
        await self._bound(db_session)
        assert await self._cause(db_session, _tray(0, state=11, remain=-1)) is None

    async def test_state9_owes_nothing(self, db_session):
        await self._bound(db_session)
        assert await self._cause(db_session, _tray(0, state=9, remain=-1)) is None

    async def test_tagless_row_owes_nothing(self, db_session):
        # A tagless tray always reports remain = -1 (no wire fullness exists, rule 8) —
        # reading it can only fail and mint a never-clearing 0700_0081.
        await self._bound(db_session, data_origin="ams_auto", tag_uid=None)
        assert await self._cause(db_session, _tray(0, state=10, remain=-1)) is None

    async def test_unbound_slot_owes_nothing(self, db_session):
        # No ledger row to repair; the pipeline binds a live tag from the push itself.
        assert await self._cause(db_session, _tray(0, state=10, remain=-1, tag=_VALID_TAG)) is None

    async def test_spent_binding_under_an_unidentified_roll_owes_discovery(self, db_session):
        await self._bound(db_session, spent_at=datetime.utcnow())
        assert await self._cause(db_session, _tray(0, state=10, remain=-1)) == "spent_occupied"

    async def test_the_sweep_commands_the_refresh_through_the_one_guarded_path(self, db_session, sessions, monkeypatch):
        # End to end: the occasion the policy opens is what identify_needed converts into
        # a verdict, and command_identify is still the only commander.
        await self._bound(db_session, weight_used=1899.9)
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = _pstate([_tray(0, state=10, remain=42, tag=_VALID_TAG)], gcode_state="FINISH", subtask_id="t1")
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)
        assert ams_presence._read_occasion_open(1, 0, 0) is False  # the read consumed it


class TestStandingUnknownBroadcast:
    """D5 (2026-08-07): ``_warn_owed_read_blocked`` only ever logged, so a slot whose
    identity had been unknown for six hours produced nothing the operator could see. It
    now also broadcasts, on the same bus and behind the SAME 1/hour/slot dedup — one
    hourly signal, never a per-pass toast storm."""

    def _client(self):
        client = MagicMock()
        client.ams_write_refusal.return_value = None
        client.ams_unit_drying.return_value = False
        client.ams_refresh_tray.return_value = (True, "ok")
        return client

    async def _defer(self, db_session, monkeypatch, printer_id):
        state = _pstate([_tray(0, state=11)], gcode_state="IDLE", tray_now=1)  # filament engaged
        _patch_pm(monkeypatch, status=state, client=self._client())
        return await ams_presence.maybe_command_owed_identify(db_session, printer_id, 0, 0, _tray(0, state=11), state)

    async def test_broadcasts_once_per_dedup_window_with_the_payload_shape(
        self, db_session, printer_factory, monkeypatch
    ):
        printer = await printer_factory(name="007-H2C")
        ws = AsyncMock()
        monkeypatch.setattr(ams_presence.ws_manager, "broadcast", ws)
        _arm_cycle(printer.id, 0, 0, age=ams_presence._OWED_READ_WARN_AFTER_S + 60)

        assert await self._defer(db_session, monkeypatch, printer.id) is False
        assert ws.await_count == 1
        assert ws.call_args.args[0] == {
            "type": "slot_standing_unknown",
            "printer_id": printer.id,
            "printer_name": "007-H2C",
            "ams_id": 0,
            "tray_id": 0,
            # WHAT is unresolved: this lane's owed identity read. The bound-presence
            # lane sends "bound_presence_unknown" on the same event and the same
            # per-slot dedup, so the toast can word itself for either.
            "case": "standing_unknown",
        }

        # Same slot again inside the re-warn window → still deferred, still silent.
        await self._defer(db_session, monkeypatch, printer.id)
        assert ws.await_count == 1

    async def test_a_recent_cycle_broadcasts_nothing(self, db_session, printer_factory, monkeypatch):
        # Below _OWED_READ_WARN_AFTER_S the defer is ordinary pacing — the next terminal
        # drains it, and the operator has nothing to do.
        printer = await printer_factory()
        ws = AsyncMock()
        monkeypatch.setattr(ams_presence.ws_manager, "broadcast", ws)
        _arm_cycle(printer.id, 0, 0, age=5)

        assert await self._defer(db_session, monkeypatch, printer.id) is False
        ws.assert_not_awaited()


# --- the spent-occupied constellation's own read (2026-08-07, spool 226) ------


def _age_slot_stamps(printer_id=1, ams_id=0, tray_id=0, *, by):
    """Backdate every stamp a commanded read left on a slot by ``by`` seconds.

    The monotonic-clock stand-in for "that read happened ``by`` seconds ago". Ages the
    ledgers TOGETHER (identity-learned, discovery, echo, cause record) because a real
    command writes them in one instant — ageing only some of them would fabricate an
    ordering the wire cannot produce."""
    key = (printer_id, ams_id, tray_id)
    for ledger in (
        ams_presence._slot_read_at,
        ams_presence._discovery_read_at,
        ams_presence._echo_pending,
        ams_presence._commanded_read_at,
    ):
        if key in ledger:
            ledger[key] -= by


async def _commanded_discovery_read(monkeypatch, *, state=10, printer_id=1):
    """Drive the REAL commander for one discovery read, and return its client.

    Deliberately not a hand-seeded ``_discovery_read_at`` write: the stamp ordering this
    accessor adjudicates (identity-learned BEFORE discovery, echo armed only on a present
    slot) is produced by ``command_identify`` and by nothing else, so the tests take it from
    there."""
    client = MagicMock()
    client.ams_refresh_tray.return_value = (True, "ok")
    _patch_pm(monkeypatch, status=_pstate([_tray(0, state=state)]), client=client)
    ok, _msg = await ams_presence.command_identify(printer_id, 0, 0, source="terminal_sweep", reason="discovery")
    assert ok is True
    return client


class TestReadAnsweredNoTag:
    """2026-08-07, spool 226 / 001-H2S slot 1 — the answer nobody consumed.

    A spent RFID-TAGGED binding sat under a fresh TAGLESS roll. The owed discovery read
    finally fired at 20:03 and answered NO TAG — the expected answer for a tagless roll —
    and that answer concluded NOTHING: the tray stayed bare, the tagless lane's
    qualified-cycle row was unreachable, and the slot stayed latched. This accessor is the
    evidence side of the fix; the conclusion is the decision table's new row."""

    def _ask(self, *, seated=True, bare=True):
        return ams_presence.read_answered_no_tag(1, 0, 0, tray_seated=seated, tray_bare=bare)

    async def test_a_slot_we_never_asked_has_no_answer(self):
        # No discovery read was ever commanded here: silence is not evidence.
        assert self._ask() is False

    async def test_a_fresh_read_has_not_answered_yet(self, monkeypatch):
        await _commanded_discovery_read(monkeypatch)
        assert self._ask() is False

    async def test_an_aged_unanswered_read_over_a_seated_bare_tray_is_a_no_tag_answer(self, monkeypatch):
        await _commanded_discovery_read(monkeypatch)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)
        assert self._ask() is True

    async def test_an_identify_still_in_flight_is_not_an_answer(self, monkeypatch):
        # The unit-scoped live signal: a read is running RIGHT NOW, so the slot's silence
        # is the cycle, not the answer.
        await _commanded_discovery_read(monkeypatch)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)
        _patch_pm(
            monkeypatch,
            status=_pstate([_tray(0, state=10)], ams_status_main=ams_presence.AMS_STATUS_IDENTIFYING),
            client=MagicMock(),
        )
        assert self._ask() is False

    async def test_a_tag_that_landed_after_the_read_is_not_a_no_tag_answer(self, monkeypatch):
        # The firmware published a valid tag for the slot → note_identity_learned re-stamps
        # _slot_read_at past the discovery stamp. The read answered WITH a tag.
        await _commanded_discovery_read(monkeypatch)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)
        ams_presence.note_identity_learned(1, 0, 0)
        assert self._ask() is False

    async def test_an_unseated_tray_is_not_an_answer(self, monkeypatch):
        # State 11 (feeding) and every non-seated shape arrive as tray_seated=False: a
        # feeding tray must never be mint-swapped.
        await _commanded_discovery_read(monkeypatch)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)
        assert self._ask(seated=False) is False

    async def test_a_configured_tray_is_not_an_answer(self, monkeypatch):
        # Config or identity asserted → not bare → the tagless/identity lanes own it.
        await _commanded_discovery_read(monkeypatch)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)
        assert self._ask(bare=False) is False

    async def test_asking_never_consumes_the_answer(self, monkeypatch):
        # Non-consuming by contract: _discovery_read_at also drives the 0700_0081 HMS
        # suppression, so popping it would let an expected read failure escape as a fault.
        await _commanded_discovery_read(monkeypatch)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)
        for _ in range(3):
            assert self._ask() is True
        assert (1, 0, 0) in ams_presence._discovery_read_at


class TestTheAnsweredReadClosesItsEntitlement:
    """A read that answered NO TAG stops being a reason to read — in EVERY binding state.

    "No tag" is an answer, not a failure to answer; for a tagless roll it is the only
    answer there is. Exactly one consumer used to conclude on it (``slot_state`` row 5a,
    spent + TAGGED bindings) and every other constellation went on re-earning reads from
    the same evidence. The loop is self-feeding, which is what made it expensive: an
    identify cycle flaps the tray present→9→present, and any flap the echo-swallow and the
    identify-explains suppression do not catch is banked as a fresh qualified gain — a new
    cycle AND a new occasion, manufactured by the read itself. Production measured 419
    identifies on one slot and 1000 on one printer in a day, each holding that printer's
    30 s identify gate so every slot decision on it deferred.
    """

    async def _answered_read(self, monkeypatch, *, state=10):
        await _commanded_discovery_read(monkeypatch, state=state)
        _age_slot_stamps(by=ams_presence._IDENTIFY_ACTIVE_S + 5)

    @pytest.mark.parametrize(
        "binding",
        ["unbound", "bound_tagged", "bound_tagless", "spent_tagged", "spent_tagless"],
        ids=lambda b: f"binding={b}",
    )
    async def test_the_answer_closes_both_read_entitlements_whatever_is_bound(self, db_session, monkeypatch, binding):
        """The closure reads no binding at all — it touches only the read ledgers — so the
        parametrization is the assertion: the same answer, the same closure, five states."""
        if binding != "unbound":
            tag = None if "tagless" in binding else _VALID_TAG
            spool = Spool(
                material="PETG",
                data_origin="rfid_auto" if tag else "ams_auto",
                tag_uid=tag,
                spent_at=datetime.utcnow() if binding.startswith("spent") else None,
            )
            db_session.add(spool)
            await db_session.flush()
            db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
            await db_session.commit()

        await self._answered_read(monkeypatch)
        # A physical cycle and an occasion, both live: exactly what a further read needs.
        ams_presence._physical_cycle_at[(1, 0, 0)] = ams_presence.time.monotonic()
        ams_presence.open_read_occasion(1, 0, 0)
        assert ams_presence.identity_unanswered(1, 0, 0) is True
        assert ams_presence._read_occasion_open(1, 0, 0) is True

        assert ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True) is True

        assert ams_presence.identity_unanswered(1, 0, 0) is False
        assert ams_presence._read_occasion_open(1, 0, 0) is False

    async def test_the_closure_is_idempotent_per_answer(self, monkeypatch):
        """It runs on every observation at ~1 Hz. The ledger pops make repeats harmless;
        the ONE-per-answer marker is what keeps the record honest."""
        await self._answered_read(monkeypatch)
        assert ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True) is True
        for _ in range(5):
            assert ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True) is False

    async def test_a_new_cause_still_earns_a_new_read(self, monkeypatch):
        """The entitlement is spent, not revoked: a fresh physical cycle is a NEW question
        about a slot somebody has physically touched, and it must still be asked."""
        await self._answered_read(monkeypatch)
        ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True)

        ams_presence._note_gain(1, 0, 0, qualified=True)
        assert ams_presence.identity_unanswered(1, 0, 0) is True
        assert ams_presence._read_occasion_open(1, 0, 0) is True

    async def test_an_unanswered_read_closes_nothing(self, monkeypatch):
        """Inside the settle window the read may still be running — silence is the cycle,
        not the answer."""
        await _commanded_discovery_read(monkeypatch)
        ams_presence._physical_cycle_at[(1, 0, 0)] = ams_presence.time.monotonic()
        ams_presence.open_read_occasion(1, 0, 0)

        assert ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True) is False
        assert ams_presence._read_occasion_open(1, 0, 0) is True

    async def test_the_row_5a_conclusion_survives_the_closure(self, monkeypatch):
        """The pipeline resolves the SAME push one step after the presence pass, and its
        spent-swap row rests on this very fact. Closing the entitlement must not destroy
        the evidence — which is why ``_slot_read_at`` is deliberately not re-stamped."""
        await self._answered_read(monkeypatch)
        assert ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True) is True
        assert ams_presence.read_answered_no_tag(1, 0, 0, tray_seated=True, tray_bare=True) is True

    async def test_the_observation_pass_is_what_closes_it_in_production(self, db_session, monkeypatch):
        """Wired where every push already goes. The answer lands ~15 s after the command,
        by which time the slot is emitting steady-state pushes and no longer edges, so the
        closure runs per OBSERVATION rather than per gain."""
        await self._answered_read(monkeypatch)
        ams_presence._physical_cycle_at[(1, 0, 0)] = ams_presence.time.monotonic()
        ams_presence.open_read_occasion(1, 0, 0)
        ams_presence._primed.add(1)  # steady state, not the first batch

        await _push(1, [{"id": 0, "tray": [_tray(0, state=10)]}], db_session)

        assert ams_presence.identity_unanswered(1, 0, 0) is False
        assert ams_presence._read_occasion_open(1, 0, 0) is False

    @pytest.mark.parametrize(("seated", "bare"), [(True, False), (False, True)], ids=["named_tray", "empty_tray"])
    async def test_it_spends_nothing_on_a_slot_whose_answer_belongs_elsewhere(self, monkeypatch, seated, bare):
        """A tray that NAMES itself was answered by the identity/tagless lanes, and an
        UNSEATED one never had a question this read could answer. Neither is a no-tag
        answer, so neither may spend an entitlement."""
        await self._answered_read(monkeypatch)
        ams_presence._physical_cycle_at[(1, 0, 0)] = ams_presence.time.monotonic()
        ams_presence.open_read_occasion(1, 0, 0)

        assert ams_presence.close_answered_read(1, 0, 0, tray_seated=seated, tray_bare=bare) is False
        assert ams_presence.identity_unanswered(1, 0, 0) is True
        assert ams_presence._read_occasion_open(1, 0, 0) is True

    async def test_the_swap_evidence_is_a_different_currency_and_is_untouched(self, monkeypatch):
        """``spool_tagless``'s pending physical cycle is the SWAP entitlement a bare tray's
        auto-configure and the table's spent-swap arms live on. Spending it here would
        starve the very transitions the answer enables."""
        from backend.app.services import spool_tagless

        await self._answered_read(monkeypatch)
        spool_tagless._pending_physical_cycles.add((1, 0, 0))

        ams_presence.close_answered_read(1, 0, 0, tray_seated=True, tray_bare=True)

        assert spool_tagless.qualified_cycle_pending(1, 0, 0) is True


class TestSpentOccupiedOccasion:
    """The spent-occupied constellation buys its OWN read occasion, once per binding epoch.

    Before this, ``spent_occupied_owed_identify`` was a standing request nothing could grant:
    occasions open only on a qualified physical cycle, a terminal sweep's between-prints
    policy, or a manual command, and spool 226's insert left none of those. The epoch key is
    the BOUND SPOOL — resolution changes it, so a genuinely new spent constellation re-opens
    naturally, while the same one never re-opens however many pushes re-derive the verdict."""

    async def test_the_first_call_buys_one_read(self):
        ams_presence.open_spent_occupied_occasion(1, 0, 0, 226)
        assert ams_presence._read_occasion_open(1, 0, 0) is True
        assert ams_presence._episode_occasion_epoch[(1, 0, 0, "spent_occupied")] == 226

    async def test_the_same_epoch_never_re_opens_after_the_read_is_spent(self, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=10)]), client=client)
        ams_presence.open_spent_occupied_occasion(1, 0, 0, 226)
        await ams_presence.command_identify(1, 0, 0, source="reconcile", reason="discovery")
        assert ams_presence._read_occasion_open(1, 0, 0) is False  # the real consumer spent it

        for _ in range(5):  # the verdict re-derives on every push
            ams_presence.open_spent_occupied_occasion(1, 0, 0, 226)
        assert ams_presence._read_occasion_open(1, 0, 0) is False  # …and buys nothing more

    async def test_a_new_epoch_re_opens(self, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=10)]), client=client)
        ams_presence.open_spent_occupied_occasion(1, 0, 0, 226)
        await ams_presence.command_identify(1, 0, 0, source="reconcile", reason="discovery")

        ams_presence.open_spent_occupied_occasion(1, 0, 0, 227)  # a different spool is bound now
        assert ams_presence._read_occasion_open(1, 0, 0) is True

    async def test_the_epoch_ledger_is_reset_state_covered(self):
        ams_presence.open_spent_occupied_occasion(1, 0, 0, 226)
        ams_presence._reset_state()
        assert ams_presence._episode_occasion_epoch == {}

    async def test_the_ambiguity_episode_buys_one_read_per_disagreement(self, monkeypatch):
        """The other episode-scoped cause, sharing the one ledger. Episode = (bound row,
        disagreeing tag): the wire re-asserts that tag at ~1 Hz, so without an epoch the
        "buy the answer" defer would re-buy the read every second."""
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=10, tag=_VALID_TAG)]), client=client)

        ams_presence.open_ambiguity_occasion(1, 0, 0, 226, "AAAA000000000001")
        assert ams_presence._read_occasion_open(1, 0, 0) is True
        await ams_presence.command_identify(1, 0, 0, source="reconcile", reason="rfid_refresh")
        assert ams_presence._read_occasion_open(1, 0, 0) is False

        for _ in range(5):  # the same disagreement, re-derived every push
            ams_presence.open_ambiguity_occasion(1, 0, 0, 226, "AAAA000000000001")
        assert ams_presence._read_occasion_open(1, 0, 0) is False

        # A genuinely DIFFERENT disagreement is a new episode and earns its own read.
        ams_presence.open_ambiguity_occasion(1, 0, 0, 226, "BBBB000000000002")
        assert ams_presence._read_occasion_open(1, 0, 0) is True

    async def test_the_two_causes_do_not_share_an_episode(self):
        """Distinct causes on one slot are distinct episodes — the ledger is keyed by both."""
        ams_presence.open_spent_occupied_occasion(1, 0, 0, 226)
        ams_presence._consume_read_occasion(1, 0, 0)
        ams_presence.open_ambiguity_occasion(1, 0, 0, 226, "AAAA000000000001")
        assert ams_presence._read_occasion_open(1, 0, 0) is True


class TestStandingOccasionDrain:
    """The reconcile drain now spends a STANDING occasion, not only an unanswered cycle.

    ``maybe_command_owed_identify``'s cheap pre-check tested ``_unanswered_cycle`` ONLY, so
    the one lane with no event dependency ignored every occasion the terminal sweep or the
    spent-occupied constellation had opened but could not command at open time (engaged
    filament, a refused wire, a missed presence edge). Spool 226's slot therefore had NO
    drain at all: the verdict re-derived forever against a read nobody would ever issue."""

    async def _spent_slot(self, db_session, *, tag=_VALID_TAG):
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=tag, spent_at=datetime.utcnow())
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        return spool

    def _idle_wire(self, monkeypatch, *, tray_state=10):
        client = MagicMock()
        client.ams_write_refusal.return_value = None
        client.ams_unit_drying.return_value = False
        client.ams_refresh_tray.return_value = (True, "ok")
        state = _pstate([_tray(0, state=tray_state)], gcode_state="IDLE", tray_now=255)
        _patch_pm(monkeypatch, status=state, client=client)
        return client, state

    async def test_a_standing_occasion_is_drained(self, db_session, monkeypatch):
        spool = await self._spent_slot(db_session)
        client, state = self._idle_wire(monkeypatch)
        ams_presence.open_spent_occupied_occasion(1, 0, 0, spool.id)
        assert ams_presence._unanswered_cycle(1, 0, 0) is False  # the OLD pre-check's whole test

        ok = await ams_presence.maybe_command_owed_identify(db_session, 1, 0, 0, _tray(0, state=10), state)

        assert ok is True
        client.ams_refresh_tray.assert_called_once_with(0, 0)
        assert ams_presence._read_occasion_open(1, 0, 0) is False  # the accepted read spent it
        # Classified ``discovery``, so its expected no-tag failure is suppressed farm-side —
        # and the stamp is exactly what ``read_answered_no_tag`` later adjudicates.
        assert (1, 0, 0) in ams_presence._discovery_read_at

    async def test_neither_entitlement_commands_nothing(self, db_session, monkeypatch):
        # No cycle and no occasion: the pre-check returns before any DB work, untouched.
        await self._spent_slot(db_session)
        client, state = self._idle_wire(monkeypatch)

        ok = await ams_presence.maybe_command_owed_identify(db_session, 1, 0, 0, _tray(0, state=10), state)

        assert ok is False
        client.ams_refresh_tray.assert_not_called()

    async def test_a_non_discovery_verdict_is_still_refused(self, db_session, monkeypatch):
        # The lane stays DISCOVERY-ONLY: an occasion over a LIVE-TAGGED tray yields
        # ``rfid_refresh``, which is the terminal sweep's between-prints business — honouring
        # it on a ~20 s reconcile cadence would re-flap every tagged tray in the fleet.
        await self._spent_slot(db_session)
        client, state = self._idle_wire(monkeypatch)
        ams_presence.open_read_occasion(1, 0, 0)
        tray = _tray(0, state=10, tag=_VALID_TAG, tray_uuid="A" * 32)
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, tray, False) == "rfid_refresh"

        ok = await ams_presence.maybe_command_owed_identify(db_session, 1, 0, 0, tray, state)

        assert ok is False
        client.ams_refresh_tray.assert_not_called()
        assert ams_presence._read_occasion_open(1, 0, 0) is True  # untouched for the sweep


class TestUnboundUnreadDiscoveryArm:
    """WS4-D3: an UNBOUND, seated, identity-less slot may buy ONE discovery read.

    The gap this closes: ``identify_needed`` answered None for a slot with no DB binding
    and no live identity even with an occasion open — the unanswered-cycle arm was the
    only unbound path, and it needs a presence EDGE the farm may never have seen (a roll
    inserted while the server was down, or during a print). Meanwhile the dispatch layers
    could not price the slot at all, so work parked behind a tray that physically held
    filament. The read IS the way out, so the constellation gets an arm.

    Storm safety is by CAUSE, not by timer: the arm needs an OPEN occasion, a commanded
    read CONSUMES it, and the only new opener is the deficit lane's once-per-episode ask.
    State 10 specifically — state 11 is the same roll threaded on to the hub, which a
    read cannot answer without unloading first.
    """

    async def _assign(self, db_session, *, tag=_VALID_TAG, tray_id=0):
        spool = Spool(material="PETG", data_origin="rfid_auto" if tag else "ams_auto", tag_uid=tag)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=tray_id))
        await db_session.commit()

    async def test_unbound_seated_identityless_with_an_occasion_owes_discovery(self, db_session):
        _arm_occasion()
        reason = await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False)
        assert reason == "discovery"

    async def test_without_an_occasion_it_owes_nothing(self, db_session):
        # The whole storm defence: the constellation is STANDING (true on every pass),
        # so re-derivation must buy nothing. Only a fresh cause opens an occasion.
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) is None

    async def test_state_eleven_earns_its_read_and_waits_for_the_idle_edge(self, db_session):
        # Feeding: the read cannot answer until the filament is unloaded, which is a
        # WIRE-SAFETY fact and is enforced by ``command_identify``'s engaged-filament
        # defer (which spends nothing, so the entitlement survives). The verdict itself
        # must match the presence the decision lanes act on — mirroring the
        # spent-occupied arm above.
        _arm_occasion()
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=11), False) == "discovery"

    async def test_a_configured_tagless_tray_owes_nothing(self, db_session):
        # Doctrine rule 4 / invariant 4: an asserted filament type IS identity. Reading
        # it can only fail, and that failure never self-clears. Not unread, not read.
        _arm_occasion()
        tray = _tray(0, state=10, tray_type="PETG")
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, tray, False) is None

    async def test_a_preset_id_alone_is_identity(self, db_session):
        _arm_occasion()
        tray = _tray(0, state=10)
        tray["tray_info_idx"] = "GFG02"
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, tray, False) is None

    async def test_a_bound_slot_does_not_take_this_arm(self, db_session):
        # The arm is for the UNBOUND constellation only; a bound slot is adjudicated by
        # the spent-occupied / rfid_refresh arms, which know what the DB claims.
        await self._assign(db_session, tag=None)
        _arm_occasion()
        assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) is None

    async def test_one_read_per_occasion_then_the_deficit_lane_blocks_the_re_ask(self, db_session, monkeypatch):
        """End to end for the tagless roll: the arm fires once, the commanded read
        consumes the occasion, and the D4 predicate then refuses to open another —
        so a genuinely tagless roll costs exactly ONE read per seating."""
        from unittest.mock import MagicMock, patch as _patch

        from backend.app.services import filament_deficit

        filament_deficit._reset_state()
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=10)]), client=client)

        manager = MagicMock()
        manager.request_evidence_pushall.return_value = True
        try:
            with _patch("backend.app.services.printer_manager.printer_manager", manager):
                assert filament_deficit.request_unread_reads(1, {(0, 0)}) == 1

            reason = await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False)
            assert reason == "discovery"
            await ams_presence.command_identify(1, 0, 0, source="reconcile", reason=reason)
            assert client.ams_refresh_tray.call_count == 1

            # The read answered "no tag" (nothing re-stamped identity beyond our own
            # command), the tray is still unread — and the lane asks for nothing more.
            assert ams_presence.read_answered_since_seating(1, 0, 0) is True
            manager.reset_mock()
            filament_deficit._reset_state()  # even a FRESH episode is refused…
            with _patch("backend.app.services.printer_manager.printer_manager", manager):
                assert filament_deficit.request_unread_reads(1, {(0, 0)}) == 0
            manager.request_evidence_pushall.assert_not_called()
            assert await ams_presence.identify_needed(db_session, 1, 0, 0, _tray(0, state=10), False) is None
        finally:
            filament_deficit._reset_state()
