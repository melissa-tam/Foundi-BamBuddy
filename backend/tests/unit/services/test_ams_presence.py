"""Mid-run AMS refill recognition — ams_presence service tests.

Covers the presence-transition tracking (immediate RFID re-read on a gain while
idle, quiet first-push seeding, no auto-unassign on loss) and the print-terminal
RFID re-read sweep. The prompt/grace machinery (``new_spool_detected``) was
deleted — tagless spools are now auto-minted/configured by ``spool_tagless`` — so
those cases are gone. The terminal sweep now RE-READS an auto-minted tagless
slot (``data_origin == "ams_auto"``) and still SKIPS an operator-bound one.
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
    """on_printer_terminal re-reads eligible unidentified slots (incl. auto-minted
    tagless), skips operator-bound + already-identified, once per transition."""

    async def test_sweeps_tagless_and_ams_auto_bound_skips_operator_bound(self, db_session, sessions, monkeypatch):
        # (0,1) bound to an auto-minted tagless spool → MUST be swept (relax).
        auto_spool = Spool(material="PETG", data_origin="ams_auto")
        # (0,2) bound to an operator/manual spool → MUST be skipped.
        manual_spool = Spool(material="PLA", data_origin="manual")
        db_session.add_all([auto_spool, manual_spool])
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=auto_spool.id, printer_id=1, ams_id=0, tray_id=1))
        db_session.add(SpoolAssignment(spool_id=manual_spool.id, printer_id=1, ams_id=0, tray_id=2))
        await db_session.commit()

        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        spacing = AsyncMock()
        monkeypatch.setattr(ams_presence, "_spacing_wait", spacing)
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={
                "ams": [
                    {
                        "id": 0,
                        "tray": [
                            _tray(0, state=11),  # eligible: present, tagless, unassigned
                            _tray(1, state=11),  # eligible: ams_auto-bound tagless (SWEPT)
                            _tray(2, state=11),  # skip: operator-bound
                            _tray(3, state=11, tag=_VALID_TAG),  # skip: already tagged
                        ],
                    },
                    {"id": 1, "tray": [_tray(0, state=0)]},  # skip: state 0 excluded
                ]
            },
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)

        assert [c.args for c in client.ams_refresh_tray.call_args_list] == [(0, 0), (0, 1)]
        assert spacing.await_count == 1  # one wait between the two reads

    async def test_state9_included_no_assignment(self, db_session, sessions, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=9)]}]},  # state 9 INCLUDED
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_called_once_with(0, 0)

    async def test_once_per_transition_dedup(self, db_session, sessions, monkeypatch):
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11)]}]},
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
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11, tag=_VALID_TAG)]}]},  # tagged only
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        await ams_presence.on_printer_terminal(1)
        client.ams_refresh_tray.assert_not_called()


class TestSpacingWait:
    async def test_early_exit_after_identifying_then_idle(self, monkeypatch):
        # ams_status_main 2 (identifying) then 0 (idle) → break after 2 polls.
        seq = iter([SimpleNamespace(ams_status_main=2), SimpleNamespace(ams_status_main=0)])
        monkeypatch.setattr(
            ams_presence.printer_manager, "get_status", lambda pid: next(seq, SimpleNamespace(ams_status_main=0))
        )
        sleep = AsyncMock()
        monkeypatch.setattr(ams_presence.asyncio, "sleep", sleep)
        await ams_presence._spacing_wait(1)
        assert sleep.await_count == 2

    async def test_full_window_when_never_identifying(self, monkeypatch):
        monkeypatch.setattr(ams_presence, "_RFID_REREAD_SPACING_S", 1.0)  # 2 polls of 0.5s
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: SimpleNamespace(ams_status_main=0))
        sleep = AsyncMock()
        monkeypatch.setattr(ams_presence.asyncio, "sleep", sleep)
        await ams_presence._spacing_wait(1)
        assert sleep.await_count == 2  # never early-exits; burns the full window


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

        # A SECOND genuine flap afterwards is acted on normally (no lingering gate).
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)
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
        # The loop's ignition: the terminal sweep re-reads a present untagged slot
        # and arms the flag; the identify flap's echo gain is then swallowed, so the
        # sweep issues exactly ONE command instead of looping every ~22 s.
        clear = AsyncMock()
        monkeypatch.setattr("backend.app.services.spool_recovery.clear_on_reinsert", clear)
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
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

        # A following gain still attempts a re-read (no phantom suppression).
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # loss
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
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())
        status = SimpleNamespace(
            state="FINISH",
            subtask_id="t1",
            ams_status_main=0,
            raw_data={"ams": [{"id": 0, "tray": [_tray(0, state=11), _tray(1, state=11)]}]},
        )
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: status)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        now = ams_presence.time.monotonic()
        ams_presence._echo_pending[(1, 0, 0)] = now  # fresh → T0 skipped
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
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())

        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
        present = _pstate([_tray(0, state=11)], gcode_state="IDLE", ams_status_main=0)
        monkeypatch.setattr(ams_presence.printer_manager, "get_status", lambda pid: present)
        monkeypatch.setattr(ams_presence.printer_manager, "get_client", lambda pid: client)

        # (1) Idle gain 9→11 → exactly ONE identify command; the in-flight flag arms.
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=9)]}], db_session)  # prime absent
        await ams_presence.on_ams_change(1, [{"id": 0, "tray": [_tray(0, state=11)]}], db_session)  # gain → re-read
        assert client.ams_refresh_tray.call_count == 1  # identify #1
        assert (1, 0, 0) in ams_presence._echo_pending

        # (2) Terminal sweep on the SAME slot must SKIP it — no second identify.
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
        monkeypatch.setattr(ams_presence, "_spacing_wait", AsyncMock())
        client = MagicMock()
        client.ams_refresh_tray.return_value = (True, "ok")
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
