"""What an AMS incident SPEAKS FOR — ``spool_recovery.owned_full_codes`` — and the entry
liveness of the per-slot overload word.

011-H2S 2026-09-29, incident 419: while the recovery driver owned a jam the operator was
paged twice —

* 17:55:24 ``0700_6200_0002_0001`` ("The AMS A Slot 3 is overloaded. The filament may be
  tangled or the filament buffer may be stuck."): its attr family (0x60) was read by no
  taxonomy row, so no incident spoke for it;
* 17:59:18 ``0700_2200_0002_0025`` (feed resistance too high, INFORMATIONAL): the push on
  which the operator's Retry cleared the actionable code, so no candidate stood beside it.

``main`` suppresses exactly the full codes the owner answers (``error.full_code in
owned``); these pin the answer. Full codes, because the short form is many-to-one: the
overload and the slot's runout demand are both ``0700_0001``.
"""

import asyncio

import pytest
from sqlalchemy import select

from backend.app.models.printer_incident import KIND_JAM, KIND_RUNOUT, STATUS_RECOVERING, PrinterIncident
from backend.app.services import printer_incidents, spool_recovery
from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.hms_errors import hms_short_code, live_candidates


def _attr(submodule: int, ams_id: int = 0) -> int:
    """An AMS-module attr: unit byte ``ams_id``, submodule byte ``submodule``."""
    return (0x07 << 24) | (ams_id << 16) | (submodule << 8)


def _hms_entry(attr: int, code_word: int, *, severity: int = 2) -> HMSError:
    """An ``hms[]``-lane entry as the parser builds it: full code = attr + code word, 16 hex."""
    return HMSError(
        code=f"0x{code_word:x}", attr=attr, module=attr >> 24, severity=severity, full_code=f"{attr:08X}{code_word:08X}"
    )


def _print_error(value: int) -> HMSError:
    """A ``print_error``-lane entry as the parser builds it: attr IS the word, full code its 8 hex."""
    return HMSError(code=f"0x{value & 0xFFFF:x}", attr=value, module=value >> 24, severity=3, full_code=f"{value:08X}")


_OVERLOAD_SLOT3 = _hms_entry(_attr(0x62), 0x00020001)  # 0700_6200_0002_0001, incident 419
_DEMAND_SLOT3 = _hms_entry(_attr(0x22), 0x00020001)  # 0700_2200_0002_0001, the runout demand
_RESISTANCE_SLOT3 = _hms_entry(_attr(0x22), 0x00020025)  # 0700_2200_0002_0025, the Retry push
_TUBE_RESISTANCE_SLOT3 = _hms_entry(_attr(0x22), 0x00020019)  # 0700_2200_0002_0019, a jam
_ASSIST_OVERLOAD = _print_error(0x07008010)  # 07008010, the 8010 jam
_RUNOUT = _print_error(0x07008011)  # 07008011, "insert into the same slot"


def _state(hms: list[HMSError], *, subtask: str = "task-1") -> PrinterState:
    state = PrinterState()
    state.connected = True
    state.report_epoch = state.connection_epoch
    state.state = "PAUSE"
    state.subtask_id = subtask
    state.tray_now = 2
    state.hms_errors = list(hms)
    return state


async def _open_row(db, printer_id: int, *, kind: str = KIND_JAM, slot_global_tray: int | None = 2) -> PrinterIncident:
    row = await printer_incidents.open_new(
        db,
        printer_id=printer_id,
        job_id="task-1",
        item_id=None,
        kind=kind,
        code="0700_8010",
        codes="mechanical_feed:0700_8010",
        slot_global_tray=slot_global_tray,
    )
    assert row is not None
    return row


@pytest.fixture(autouse=True)
def _reset_recovery():
    spool_recovery._reset_state()
    yield
    spool_recovery._reset_state()


@pytest.mark.asyncio
class TestFullCodesNeverShortCodes:
    async def test_the_pair_collides_on_its_short_form(self):
        """The premise: the lossy form cannot tell the two apart."""
        assert hms_short_code(_OVERLOAD_SLOT3.attr, _OVERLOAD_SLOT3.code) == "0700_0001"
        assert hms_short_code(_DEMAND_SLOT3.attr, _DEMAND_SLOT3.code) == "0700_0001"

    @pytest.mark.parametrize("row_open", [False, True], ids=["entry-would-open", "row-standing"])
    async def test_an_owned_overload_never_owns_the_runout_demand(self, db_session, printer_factory, row_open):
        printer = await printer_factory()
        if row_open:
            await _open_row(db_session, printer.id)

        owned = await spool_recovery.owned_full_codes(db_session, printer.id, _state([_OVERLOAD_SLOT3, _DEMAND_SLOT3]))

        assert owned == {_OVERLOAD_SLOT3.full_code}

    async def test_a_runout_incident_speaks_for_the_demand(self, db_session, printer_factory):
        """The slot-attributed runout words are the RUNOUT class's companions: beside the
        runout code, the demand is carried by the runout incident's own guidance."""
        printer = await printer_factory()

        owned = await spool_recovery.owned_full_codes(db_session, printer.id, _state([_RUNOUT, _DEMAND_SLOT3]))

        assert owned == {_RUNOUT.full_code, _DEMAND_SLOT3.full_code}

    async def test_a_standing_runout_row_speaks_for_the_demand(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open_row(db_session, printer.id, kind=KIND_RUNOUT)

        owned = await spool_recovery.owned_full_codes(db_session, printer.id, _state([_ASSIST_OVERLOAD, _DEMAND_SLOT3]))

        assert owned == {_ASSIST_OVERLOAD.full_code, _DEMAND_SLOT3.full_code}

    async def test_a_jam_never_speaks_for_a_demand(self, db_session, printer_factory):
        """A jam's messages say nothing about filling a slot, so the demand beside it pages
        — the same rule as the overload, whichever jam word owns the printer."""
        printer = await printer_factory()

        owned = await spool_recovery.owned_full_codes(db_session, printer.id, _state([_ASSIST_OVERLOAD, _DEMAND_SLOT3]))

        assert owned == {_ASSIST_OVERLOAD.full_code}

    async def test_both_wire_lanes_answer_their_own_full_code(self, db_session, printer_factory):
        printer = await printer_factory()

        owned = await spool_recovery.owned_full_codes(
            db_session, printer.id, _state([_ASSIST_OVERLOAD, _TUBE_RESISTANCE_SLOT3])
        )

        assert owned == {"07008010", "0700220000020019"}

    async def test_an_entry_with_no_full_code_is_never_owned(self, db_session, printer_factory):
        printer = await printer_factory()
        nameless = HMSError(code="8010", attr=0x07000000, module=7, severity=2)

        assert await spool_recovery.owned_full_codes(db_session, printer.id, _state([nameless])) == frozenset()


@pytest.mark.asyncio
class TestNoticesAreOwnedByTheOpenRowsUnit:
    """The authority for an INFORMATIONAL notice is the OPEN AMS row naming its unit —
    never same-push candidates, because the operator's Retry is exactly the push on
    which the actionable code clears (011-H2S 17:59:18)."""

    @pytest.mark.parametrize(
        "notice",
        [
            _RESISTANCE_SLOT3,  # the 0x00020025 code-word row
            _hms_entry(_attr(0x20), 0x00030007),  # "Checking the filament location…"
            _hms_entry(_attr(0x01), 0x00020025),  # the 07xx_0025 SHORT row (no code-word row reads 0x01)
        ],
        ids=["feed-resistance", "checking-location", "short-row-0025"],
    )
    async def test_the_retry_push_is_owned_by_the_open_jam_row(self, db_session, printer_factory, notice):
        printer = await printer_factory()
        await _open_row(db_session, printer.id, slot_global_tray=2)  # AMS 0, slot 3
        state = _state([notice])
        assert not live_candidates(state), "the 011 shape: no actionable code on this push"

        assert await spool_recovery.owned_full_codes(db_session, printer.id, state) == {notice.full_code}

    async def test_with_no_open_row_the_notice_pages(self, db_session, printer_factory):
        printer = await printer_factory()

        assert await spool_recovery.owned_full_codes(db_session, printer.id, _state([_RESISTANCE_SLOT3])) == frozenset()

    async def test_a_notice_on_another_ams_unit_pages(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open_row(db_session, printer.id, slot_global_tray=2)  # AMS 0
        other_unit = _hms_entry(_attr(0x22, ams_id=1), 0x00020025)

        assert await spool_recovery.owned_full_codes(db_session, printer.id, _state([other_unit])) == frozenset()

    async def test_a_row_naming_no_slot_names_no_unit(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open_row(db_session, printer.id, slot_global_tray=None)

        assert await spool_recovery.owned_full_codes(db_session, printer.id, _state([_RESISTANCE_SLOT3])) == frozenset()

    async def test_the_notice_rides_beside_the_candidates_the_row_owns(self, db_session, printer_factory):
        printer = await printer_factory()
        await _open_row(db_session, printer.id, slot_global_tray=2)

        owned = await spool_recovery.owned_full_codes(
            db_session, printer.id, _state([_ASSIST_OVERLOAD, _RESISTANCE_SLOT3])
        )

        assert owned == {_ASSIST_OVERLOAD.full_code, _RESISTANCE_SLOT3.full_code}


@pytest.mark.asyncio
class TestFailsTowardNotifying:
    """Invariant 10: a suppression predicate that cannot answer owns nothing."""

    async def test_a_push_with_no_candidate_and_no_notice_reads_nothing(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        rfid = _print_error(0x07004025)  # "Failed to read the filament information." — RFID_READ
        spy = []

        async def _get_open(*args, **kwargs):
            spy.append(args)
            return None

        monkeypatch.setattr(printer_incidents, "get_open", _get_open)

        assert await spool_recovery.owned_full_codes(db_session, printer.id, _state([rfid])) == frozenset()
        assert spy == []

    async def test_a_crashed_read_owns_nothing(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()

        async def _boom(*_a, **_k):
            raise RuntimeError("db down")

        monkeypatch.setattr(printer_incidents, "get_open", _boom)

        state = _state([_ASSIST_OVERLOAD, _RESISTANCE_SLOT3])
        assert await spool_recovery.owned_full_codes(db_session, printer.id, state) == frozenset()

    async def test_recovery_disabled_owns_nothing(self, db_session, printer_factory, monkeypatch):
        printer = await printer_factory()
        await _open_row(db_session, printer.id, slot_global_tray=2)

        async def _disabled(_db, key, default):
            return False if key == "spool_recovery_enabled" else default

        monkeypatch.setattr(spool_recovery, "_read_bool", _disabled)

        state = _state([_ASSIST_OVERLOAD, _RESISTANCE_SLOT3])
        assert await spool_recovery.owned_full_codes(db_session, printer.id, state) == frozenset()


@pytest.mark.asyncio
class TestALoneOverloadDrivesAJam:
    """Liveness: the overload word, standing ALONE (no ``0700_8010`` beside it), opens and
    drives a jam incident at the entry exactly as its 8010 twin does — on the slot its own
    attr names."""

    @pytest.mark.parametrize(
        ("ams_id", "tray", "global_tray"),
        [(0, 2, 2), (1, 1, 5)],
        ids=["AMS-A-slot-3", "AMS-B-slot-2"],
    )
    async def test_it_opens_a_recovering_jam_on_its_slot(
        self, db_session, printer_factory, own_session_factory, monkeypatch, ams_id, tray, global_tray
    ):
        import backend.app.core.database as core_db

        monkeypatch.setattr(core_db, "async_session", own_session_factory)
        printer = await printer_factory()
        overload = _hms_entry(_attr(0x60 + tray, ams_id=ams_id), 0x00020001)

        task = await spool_recovery.on_ams_fault(printer.id, _state([overload]))

        assert task is not None, "a driver-bound jam spawns its driver"
        assert printer_incidents.driver_live(printer.id)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        rows = (await db_session.execute(select(PrinterIncident))).scalars().all()
        assert [(r.printer_id, r.kind, r.status, r.slot_global_tray) for r in rows] == [
            (printer.id, KIND_JAM, STATUS_RECOVERING, global_tray)
        ]
        assert rows[0].codes == f"mechanical_feed:070{ams_id}_0001@{ams_id}-{tray}"
        assert rows[0].hms_full_codes == overload.full_code
