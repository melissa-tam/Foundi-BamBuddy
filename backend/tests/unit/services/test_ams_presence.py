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

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence

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


def _pstate(trays, *, ams_id=0, gcode_state="IDLE", subtask_id="task-1", ams_status_main=0):
    return SimpleNamespace(
        state=gcode_state,
        subtask_id=subtask_id,
        ams_status_main=ams_status_main,
        raw_data={"ams": [{"id": ams_id, "tray": trays}]},
    )


def _arm_cycle(printer_id=1, ams_id=0, tray_id=0, *, age=0.0):
    """Record an unanswered QUALIFIED physical cycle for a slot — what a >=5 s
    pull-and-reseat leaves behind — without replaying the whole presence sequence.
    The end-to-end path (loss → backdated absence → gain) is pinned separately."""
    ams_presence._physical_cycle_at[(printer_id, ams_id, tray_id)] = ams_presence.time.monotonic() - age


async def _physically_cycle(db_session, printer_id=1, ams_id=0, tray_id=0, *, tray=None):
    """Drive a REAL qualified physical cycle through on_ams_change: seed present,
    observe the loss, backdate the absence past _MIN_PHYSICAL_ABSENT_S, then gain."""
    seated = tray if tray is not None else _tray(tray_id, state=11)
    await ams_presence.on_ams_change(printer_id, [{"id": ams_id, "tray": [seated]}], db_session)  # seed present
    await ams_presence.on_ams_change(
        printer_id, [{"id": ams_id, "tray": [_tray(tray_id, state=9)]}], db_session
    )  # pulled
    ams_presence._absent_since[(printer_id, ams_id, tray_id)] = ams_presence.time.monotonic() - (
        ams_presence._MIN_PHYSICAL_ABSENT_S + 1
    )
    await ams_presence.on_ams_change(printer_id, [{"id": ams_id, "tray": [seated]}], db_session)  # reseated


def _patch_pm(monkeypatch, *, status=None, client=None):
    monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
    monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)


class TestPresenceTracking:
    """on_ams_change presence transitions (steady state, after the first push)."""

    async def test_gain_while_idle_rereads(self, db_session, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        # First push primes (quiet — no re-read even though the slot is present).
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        client.ams_refresh_tray.assert_not_called()

        # Second push: physical insert 9→11 while idle → immediate re-read.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_first_push_seeds_quietly(self, db_session, monkeypatch):
        # A present-but-unidentified slot on the very first push must NOT re-read
        # (a refill done while the server was down is seeded, not acted on).
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        client.ams_refresh_tray.assert_not_called()

    async def test_gain_during_print_takes_no_action(self, db_session, monkeypatch):
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="RUNNING"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        client.ams_refresh_tray.reset_mock()
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain mid-print
        client.ams_refresh_tray.assert_not_called()  # ams_get_rfid never fired during a print

    async def test_no_rereads_without_gain(self, db_session, monkeypatch):
        # Already-present slot that stays present → no re-read (no rising edge).
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # prime present
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # still present
        client.ams_refresh_tray.assert_not_called()

    async def test_presence_loss_keeps_assignment(self, db_session, monkeypatch):
        # A spool pulled for drying keeps its assignment — NO silent auto-unassign.
        db_session.add(SpoolAssignment(spool_id=1, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        from sqlalchemy import select

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # prime present
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss

        res = await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == 1))
        assert res.scalar_one_or_none() is not None  # assignment survived the removal


class TestOutOfRotationClear:
    """on_ams_change fires spool_recovery.clear_on_reinsert on a presence GAIN
    edge (physical re-insert), NOT on the first-push seed, and NOT idle-gated."""

    async def test_gain_edge_invokes_clear(self, db_session, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        # Prime absent (state 9), then physical insert 9→11 → clear fires once.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        spy.assert_not_awaited()
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
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
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        spy.assert_not_awaited()

    async def test_gain_during_print_still_clears(self, db_session, monkeypatch):
        # NOT idle-gated: a spool untangled and re-seated mid-print clears too,
        # even though the idle-only RFID re-read stays suppressed during a print.
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="RUNNING"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
        spy.assert_awaited_once()
        client.ams_refresh_tray.assert_not_called()  # idle re-read still suppressed mid-print


class TestTerminalSweep:
    """on_printer_terminal commands exactly the identifies identify_needed asks for:
    tagged slots (live or DB-bound) every terminal, physically-changed slots once,
    and NOTHING for an untouched tagless slot — once per terminal transition."""

    async def test_reads_tagged_and_changed_slots_only(self, db_session, sessions, monkeypatch):
        # (0,1) bound to an auto-minted TAGLESS spool + physically cycled → discovery.
        auto_spool = Spool(material="PETG", data_origin="ams_auto")
        # (0,2) bound to a spool that carries an RFID identity → always refreshed.
        tagged_spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        db_session.add_all([auto_spool, tagged_spool])
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=auto_spool.id, printer_id=1, ams_id=0, tray_id=1))
        db_session.add(SpoolAssignment(spool_id=tagged_spool.id, printer_id=1, ams_id=0, tray_id=2))
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
                            _tray(2, state=11),  # rfid_refresh: DB-bound to a tagged spool
                            _tray(3, state=11, tag=_VALID_TAG),  # rfid_refresh: live tag
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
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11, tag=_VALID_TAG)]}]},  # always needed
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
    arrives as a fresh gain — the command's own echo — which on_ams_change would
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
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        assert client.ams_refresh_tray.call_count == 1
        assert clear.await_count == 1
        assert (1, 0, 0) in ams_presence._echo_pending  # armed on success

        # Identify flap: loss 11→9 then settle-back 9→11 — THIS gain is our echo.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
        assert client.ams_refresh_tray.call_count == 1  # echo swallowed — no 2nd re-read
        assert clear.await_count == 1  # feed-fault clear NOT re-run for the echo
        assert (1, 0, 0) not in ams_presence._echo_pending  # flag consumed

        # A SECOND genuine pull+reseat afterwards is acted on normally (no lingering
        # gate). Its absence is backdated past _MIN_PHYSICAL_ABSENT_S: only a real
        # physical cycle is discovery evidence, a sub-second state flap is not.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
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
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # insert
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
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
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

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(
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

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain (refused)
        assert client.ams_refresh_tray.call_count == 1
        assert ams_presence._echo_pending == {}  # refused → nothing armed

        # A following physical cycle still attempts a re-read (no phantom suppression):
        # a refused command learned nothing, so the change is still unanswered.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
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

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # stale gain

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
        # remain% for gram tracking + reused-core detection ride on this read.
        assert await self._needed(db_session, _tray(0, state=11, tag=_VALID_TAG)) == "rfid_refresh"

    async def test_db_bound_tagged_is_refreshed(self, db_session):
        spool = Spool(material="PETG", data_origin="rfid_auto", tag_uid=_VALID_TAG)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=1, ams_id=0, tray_id=0))
        await db_session.commit()
        assert await self._needed(db_session, _tray(0, state=11)) == "rfid_refresh"

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
        from backend.app.services import spool_tagless

        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())

        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        client.wait_ams_settle = AsyncMock(return_value=True)
        present = _pstate([_tray(0, state=11)], gcode_state="IDLE", ams_status_main=0)
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: present)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        # (1) Idle gain 9→11 → exactly ONE identify command; the in-flight flag arms.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain → re-read
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

        # (3) Tagless config on the SAME slot must DEFER — nothing minted, no write.
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
        handled = await spool_tagless.handle_tagless_slot(db_session, 1, 0, 0, tray, None, [])
        assert handled is True  # deferred → caller `continue`s (no respool-gate fall-through)
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
    drying, on_ams_change must NOT clear a feed-fault flag and NOT fire an idle
    re-read, and the terminal sweep must skip the drying unit — a re-read would
    disengage the tray and fail the cycle (HMS 0700_C069)."""

    async def test_clear_on_reinsert_skipped_while_drying(self, db_session, monkeypatch):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        spy = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", spy)
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # drying flap gain
        spy.assert_not_awaited()  # feed-fault clear NOT run for a drying flap
        assert ams_presence._last_presence[(1, 0, 0)] is True  # presence map still updated

    async def test_idle_reread_skipped_while_drying(self, db_session, monkeypatch):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # drying flap
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

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)
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
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
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
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # genuine gain
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

        await ams_presence.on_ams_change(
            1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session
        )  # first push seeds present
        await ams_presence.on_ams_change(
            1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session
        )  # loss -> stamps absence
        # Backdate the absence past the physical-swap threshold.
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - (
            ams_presence._MIN_PHYSICAL_ABSENT_S + 1
        )
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain
        _spy_note.assert_awaited_once_with(1, 0, 0)

    async def test_short_flap_does_not_fire(self, db_session, monkeypatch, _spy_note):
        # 16 ms flap (a runout-instant state flap) -> absence < 5 s -> no cycle.
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss (stamp now)
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain, ~0 s later
        _spy_note.assert_not_awaited()

    async def test_first_push_seed_never_fires(self, db_session, monkeypatch, _spy_note):
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)]), client=client)
        await ams_presence.on_ams_change(
            1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session
        )  # first push, present
        _spy_note.assert_not_awaited()

    async def test_echo_gain_does_not_fire(self, db_session, monkeypatch, _spy_note):
        # An identify-flap echo gain is swallowed before the cycle note (an echo is not
        # a physical event) even though the backdated absence would otherwise qualify.
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=11)], gcode_state="IDLE"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        ams_presence._echo_pending[(1, 0, 0)] = ams_presence.time.monotonic()  # arm the echo flag
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # echo gain
        _spy_note.assert_not_awaited()

    async def test_drying_gain_does_not_fire(self, db_session, monkeypatch, _spy_note):
        monkeypatch.setattr(ams_presence, "unit_drying", lambda pid, aid: True)
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", AsyncMock())
        client = MagicMock()
        _patch_pm(monkeypatch, status=_pstate([_tray(0, state=9)], gcode_state="IDLE"), client=client)

        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # seed present
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
        ams_presence._absent_since[(1, 0, 0)] = ams_presence.time.monotonic() - 10
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain while drying
        _spy_note.assert_not_awaited()
