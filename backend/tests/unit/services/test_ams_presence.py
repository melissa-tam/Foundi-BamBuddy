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


@pytest.fixture(autouse=True)
def _clean_state():
    ams_presence._reset_state()
    yield
    ams_presence._reset_state()


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
