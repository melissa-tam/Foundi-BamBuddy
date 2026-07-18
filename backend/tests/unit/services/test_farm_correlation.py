"""Verdict-matrix tests for the terminal-status → queue-item correlation (Phase 1).

Covers all five verdicts of :func:`resolve_terminal_item` — including the
upgrade-day NULL-key single-candidate fallback, the present-id-matches-nothing
foreign case, and the ZERO-candidate-with-echoed-id foreign case (the production
S4 stall: farm units cancelled, then the farm's own USB file re-started from the
touchscreen) — plus the farm-work-targets-printer helper that drives the
conditional plate-gate raise. FK enforcement is off in the test engine, so rows
reference arbitrary printer/sku ids without seeding those parents.
"""

from datetime import datetime, timezone

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.farm_correlation import (
    classify_stop,
    farm_work_targets_printer,
    on_native_plate_detection,
    resolve_active_plate_id,
    resolve_terminal_item,
)
from backend.app.services.printer_manager import printer_manager


async def _add_item(
    db,
    *,
    printer_id,
    status="printing",
    dispatch_subtask_id=None,
    library_file_id=None,
    batch_id=None,
    plate_id=None,
):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=False,
        dispatch_subtask_id=dispatch_subtask_id,
        library_file_id=library_file_id,
        batch_id=batch_id,
        plate_id=plate_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


async def _add_library_file(db, filename):
    lf = LibraryFile(filename=filename, file_path=f"/lib/{filename}", file_type="3mf", file_size=1)
    db.add(lf)
    await db.commit()
    await db.refresh(lf)
    return lf


async def _add_farm_batch(db):
    batch = PrintBatch(name="run", sku_file_id=1)  # sku_file_id set == a farm batch
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch


class TestResolveTerminalItemVerdicts:
    """The five-verdict matrix."""

    async def test_matched_by_subtask_id(self, db_session):
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1")
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "SUB-1", "subtask_name": "anything"})
        assert res.verdict == "matched"
        assert res.item.id == item.id

    async def test_subtask_id_wins_over_name(self, db_session):
        # An id match short-circuits before name matching even when the name differs.
        lf = await _add_library_file(db_session, "WidgetA.gcode.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1", library_file_id=lf.id)
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "SUB-1", "subtask_name": "Different"})
        assert res.verdict == "matched"
        assert res.item.id == item.id

    async def test_stamped_item_same_name_different_id_is_foreign(self, db_session):
        # S4/S9 tightening: the operator re-prints the SAME file locally from the
        # touchscreen — firmware mints a fresh subtask_id but the name is identical
        # to the farm item's file. A STAMPED item can only be claimed by id
        # equality, so this terminal is FOREIGN and the farm unit stays untouched.
        lf = await _add_library_file(db_session, "WidgetA.gcode.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1", library_file_id=lf.id)
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "OTHER", "subtask_name": "WidgetA"})
        assert res.verdict == "foreign"
        assert res.item is None
        await db_session.refresh(item)
        assert item.status == "printing"  # resolver never hands the unit over

    async def test_unstamped_item_rescued_by_name_with_present_id(self, db_session):
        # Upgrade-day rescue: a row dispatched BEFORE dispatch_subtask_id existed
        # (NULL key) plus a terminal that does echo an id. Id equality can't match
        # a NULL key, but the dispatched-name match still binds the finish to the
        # legacy row.
        lf = await _add_library_file(db_session, "WidgetA.gcode.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id=None, library_file_id=lf.id)
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "ECHOED-1", "subtask_name": "WidgetA"})
        assert res.verdict == "matched_by_name"
        assert res.item.id == item.id

    async def test_fallback_single_candidate_no_payload_id(self, db_session):
        # Upgrade-day / firmware-no-echo: no subtask_id on the terminal, one printing
        # item (here with a NULL dispatch_subtask_id) → best-effort fallback.
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id=None)
        res = await resolve_terminal_item(db_session, 1, {"subtask_name": "Whatever", "filename": "x.gcode"})
        assert res.verdict == "fallback"
        assert res.item.id == item.id

    async def test_foreign_present_id_matches_nothing(self, db_session):
        # A LOCAL print's finish: its subtask_id matches no printing item and its
        # name doesn't either → foreign, item None (farm state must not be mutated).
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1")
        res = await resolve_terminal_item(
            db_session, 1, {"subtask_id": "FOREIGN-9", "subtask_name": "OperatorLocal", "filename": "local.gcode"}
        )
        assert res.verdict == "foreign"
        assert res.item is None

    async def test_none_when_nothing_printing_and_no_id(self, db_session):
        # Zero printing candidates AND no echoed subtask_id → none (a bare state blip
        # we must never guess a foreign deposit from). A completed row is not a candidate.
        await _add_item(db_session, printer_id=1, status="completed", dispatch_subtask_id="SUB-1")
        res = await resolve_terminal_item(db_session, 1, {"subtask_name": "Test", "filename": "x.gcode"})
        assert res.verdict == "none"
        assert res.item is None

    async def test_foreign_when_nothing_printing_but_id_echoed(self, db_session):
        # The production S4 case: every farm unit was cancelled (no printing row), then
        # the operator re-started the farm's own USB file from the touchscreen — a fresh
        # id echoed with ZERO printing candidates. That is FOREIGN, not silent "none":
        # the caller must gate + watch + alert, never strand the printer silently.
        await _add_item(db_session, printer_id=1, status="cancelled", dispatch_subtask_id="OLD-1")
        res = await resolve_terminal_item(
            db_session, 1, {"subtask_id": "SCREEN-9", "subtask_name": "FarmFile", "filename": "farmfile.gcode"}
        )
        assert res.verdict == "foreign"
        assert res.item is None

    async def test_none_when_nothing_printing_empty_id_is_blank(self, db_session):
        # A present-but-blank subtask_id strips to None → treated as no id → none.
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "   ", "subtask_name": "Test"})
        assert res.verdict == "none"
        assert res.item is None

    async def test_only_considers_target_printer(self, db_session):
        # A printing item on ANOTHER printer must not be attributed here. For printer 1
        # there are ZERO candidates and an id is echoed → foreign (item None), NEVER
        # reaching over to printer 2's item.
        await _add_item(db_session, printer_id=2, dispatch_subtask_id="SUB-1")
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "SUB-1"})
        assert res.verdict == "foreign"
        assert res.item is None


class TestFarmWorkTargetsPrinter:
    """The helper that decides whether a printer has farm work bound to it."""

    async def test_true_for_pending_farm_item(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=5, status="pending", batch_id=batch.id)
        assert await farm_work_targets_printer(db_session, 5) is True

    async def test_true_for_printing_farm_item(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=5, status="printing", batch_id=batch.id)
        assert await farm_work_targets_printer(db_session, 5) is True

    async def test_false_for_non_farm_batch(self, db_session):
        plain = PrintBatch(name="plain")  # no sku_file_id → not a farm batch
        db_session.add(plain)
        await db_session.commit()
        await db_session.refresh(plain)
        await _add_item(db_session, printer_id=5, status="pending", batch_id=plain.id)
        assert await farm_work_targets_printer(db_session, 5) is False

    async def test_false_when_farm_item_targets_other_printer(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=9, status="pending", batch_id=batch.id)
        assert await farm_work_targets_printer(db_session, 5) is False

    async def test_false_when_farm_item_is_terminal(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=5, status="completed", batch_id=batch.id)
        assert await farm_work_targets_printer(db_session, 5) is False


class TestResolveActivePlateId:
    """The print-start / archive-creation plate-id resolver (#1697)."""

    async def test_subtask_match_returns_its_plate_id(self, db_session):
        # Two printing items; the one whose dispatch_subtask_id matches wins,
        # even though the other started more recently.
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="OTHER", plate_id=7)
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1", plate_id=2)
        assert await resolve_active_plate_id(db_session, 1, "SUB-1") == 2

    async def test_sole_printing_item_when_no_subtask(self, db_session):
        await _add_item(db_session, printer_id=1, dispatch_subtask_id=None, plate_id=3)
        assert await resolve_active_plate_id(db_session, 1, None) == 3

    async def test_sole_printing_item_when_subtask_matches_nothing(self, db_session):
        # An echoed id that matches no item still resolves to the sole printing
        # unit — plate scoping is best-effort, not identity-gated like terminals.
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1", plate_id=4)
        assert await resolve_active_plate_id(db_session, 1, "NOPE") == 4

    async def test_no_match_multiple_candidates_returns_none(self, db_session):
        # Two un-id-matched printing items → genuinely ambiguous → None.
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="A", plate_id=1)
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="B", plate_id=2)
        assert await resolve_active_plate_id(db_session, 1, "ZZZ") is None

    async def test_nothing_printing_returns_none(self, db_session):
        await _add_item(db_session, printer_id=1, status="completed", plate_id=5)
        assert await resolve_active_plate_id(db_session, 1, None) is None

    async def test_null_plate_id_passes_through(self, db_session):
        # A matched item whose plate_id is None (single-plate / non-farm) returns None.
        await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1", plate_id=None)
        assert await resolve_active_plate_id(db_session, 1, "SUB-1") is None

    async def test_only_considers_target_printer(self, db_session):
        await _add_item(db_session, printer_id=2, dispatch_subtask_id="SUB-1", plate_id=9)
        assert await resolve_active_plate_id(db_session, 1, "SUB-1") is None


class TestClassifyStop:
    """Pure operator-stop classification (Phase 3.1) — no DB, no I/O."""

    def test_ui_membership(self):
        assert classify_stop({}, 1, {1}) == "operator_ui"

    def test_screen_echo(self):
        assert classify_stop({"user_cancel_observed": True}, 1, set()) == "operator_screen"

    def test_ui_wins_over_screen(self):
        # Both signals present → UI membership wins.
        assert classify_stop({"user_cancel_observed": True}, 1, {1}) == "operator_ui"

    def test_neither_is_none(self):
        assert classify_stop({}, 1, set()) is None

    def test_no_echo_key_is_none(self):
        # A reconcile-synthesised payload carries neither signal.
        assert classify_stop({"status": "aborted"}, 1, {2, 3}) is None

    def test_false_echo_is_none(self):
        assert classify_stop({"user_cancel_observed": False}, 1, set()) is None


async def _add_eject_item(db, *, printer_id, status="printing", eject_profile_id=None, batch_id=None):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=False,
        eject_profile_id=eject_profile_id,
        batch_id=batch_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


class TestNativePlateDetection:
    """Native pre-print vision capture (Phase 3.3): a NEW plate-occupancy HMS code
    while a farm unit is printing → human-clear-only gate + flagged unit."""

    async def test_farm_item_gets_gate_and_flag(self, db_session):
        batch = await _add_farm_batch(db_session)  # sku_file_id set == farm
        item = await _add_eject_item(db_session, printer_id=11, batch_id=batch.id)
        try:
            flagged = await on_native_plate_detection(db_session, 11, {"0300_8017"})
            assert flagged is True
            await db_session.refresh(item)
            assert item.waiting_reason == "plate_not_empty_printer_detected"
            assert item.status == "printing"  # NEVER terminal
            # Human-clear-only gate raised via the canonical setter.
            assert printer_manager.is_awaiting_plate_clear(11) is True
        finally:
            printer_manager.set_awaiting_plate_clear(11, False)

    async def test_eject_profile_item_is_farm(self, db_session):
        # A non-sku batch but an item carrying an eject_profile_id still counts as farm.
        item = await _add_eject_item(db_session, printer_id=12, eject_profile_id=7)
        try:
            flagged = await on_native_plate_detection(db_session, 12, {"0300_8006"})
            assert flagged is True
            await db_session.refresh(item)
            assert item.waiting_reason == "plate_not_empty_printer_detected"
        finally:
            printer_manager.set_awaiting_plate_clear(12, False)

    async def test_non_farm_printer_no_gate(self, db_session):
        # A plain (non-farm) printing item → no gate, no flag.
        item = await _add_eject_item(db_session, printer_id=13)  # no eject profile, no farm batch
        flagged = await on_native_plate_detection(db_session, 13, {"0300_8017"})
        assert flagged is False
        await db_session.refresh(item)
        assert item.waiting_reason is None
        assert printer_manager.is_awaiting_plate_clear(13) is False

    async def test_nothing_printing_no_gate(self, db_session):
        flagged = await on_native_plate_detection(db_session, 14, {"0300_8017"})
        assert flagged is False
        assert printer_manager.is_awaiting_plate_clear(14) is False

    async def test_plate_offset_808c_triggers_reaction(self, db_session):
        """C4: HMS 0500_808C (build-plate offset / debris) is a plate-occupancy code
        and drives the same human-clear-only gate + waiting_reason as 0300_8017."""
        batch = await _add_farm_batch(db_session)
        item = await _add_eject_item(db_session, printer_id=15, batch_id=batch.id)
        try:
            flagged = await on_native_plate_detection(db_session, 15, {"0500_808C"})
            assert flagged is True
            await db_session.refresh(item)
            assert item.waiting_reason == "plate_not_empty_printer_detected"
            assert item.status == "printing"
            assert printer_manager.is_awaiting_plate_clear(15) is True
        finally:
            printer_manager.set_awaiting_plate_clear(15, False)

    def test_808c_is_in_plate_occupancy_set(self):
        """0500_808C joined the single-origin plate-occupancy frozenset, so BOTH the
        main.py capture hook and the failure-reason attribution pick it up."""
        from backend.app.services.bambu_mqtt import _HMS_PLATE_OCCUPANCY_CODES

        assert "0500_808C" in _HMS_PLATE_OCCUPANCY_CODES
        assert {"0300_8017", "0300_8006"} <= _HMS_PLATE_OCCUPANCY_CODES
