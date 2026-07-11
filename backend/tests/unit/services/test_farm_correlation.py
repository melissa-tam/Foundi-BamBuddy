"""Verdict-matrix tests for the terminal-status → queue-item correlation (Phase 1).

Covers all five verdicts of :func:`resolve_terminal_item` — including the
upgrade-day NULL-key single-candidate fallback and the present-id-matches-nothing
foreign case — plus the farm-work-targets-printer helper that drives the
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
):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=False,
        dispatch_subtask_id=dispatch_subtask_id,
        library_file_id=library_file_id,
        batch_id=batch_id,
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

    async def test_none_when_nothing_printing(self, db_session):
        # A completed row is not a candidate; nothing is printing on this printer.
        await _add_item(db_session, printer_id=1, status="completed", dispatch_subtask_id="SUB-1")
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "SUB-1"})
        assert res.verdict == "none"
        assert res.item is None

    async def test_only_considers_target_printer(self, db_session):
        # A printing item on ANOTHER printer must not be attributed here.
        await _add_item(db_session, printer_id=2, dispatch_subtask_id="SUB-1")
        res = await resolve_terminal_item(db_session, 1, {"subtask_id": "SUB-1"})
        assert res.verdict == "none"
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
