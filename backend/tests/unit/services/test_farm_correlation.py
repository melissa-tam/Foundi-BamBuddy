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
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.dispatch_target import encode_printer_ids
from backend.app.services.farm_correlation import (
    STOP_VERDICT_PLATE_REFUSED,
    classify_stop,
    farm_work_slated_for,
    farm_work_targets_printer,
    resolve_active_plate_id,
    resolve_printing_farm_item,
    resolve_terminal_item,
    terminal_disposition,
    upgrade_to_foreign_auto_eject,
)
from backend.app.services.hms_errors import PrinterMessage
from backend.app.services.plate_occupancy import (
    CooldownEject,
    DepositEvidence,
    EscalationOnly,
    Evidence,
    ForeignAutoEject,
    PlateRefusal,
    plate_occupancy,
)
from backend.app.services.printer_manager import printer_manager

# The per-file occupancy reset that used to live here is GONE: the plate-vision
# reaction it protected against moved to ``pause_recovery``, and conftest's autouse
# ``reset_plate_occupancy_authority`` already starts and leaves every test with an
# empty, un-wired authority. A second reset of one process singleton is exactly the
# duplication the 2026-08-30 wave removed everywhere else.


async def _add_item(
    db,
    *,
    printer_id,
    status="printing",
    dispatch_subtask_id=None,
    library_file_id=None,
    batch_id=None,
    plate_id=None,
    target_model=None,
    target_printer_ids=None,
):
    item = PrintQueueItem(
        printer_id=printer_id,
        status=status,
        first_article=False,
        dispatch_subtask_id=dispatch_subtask_id,
        library_file_id=library_file_id,
        batch_id=batch_id,
        plate_id=plate_id,
        target_model=target_model,
        target_printer_ids=target_printer_ids,
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

    async def test_spliced_corpus_name_rescues_unstamped_item(self, db_session):
        """2026-08-22: the splicer writes a MID-STEM ``.gcode`` token that the firmware
        drops from its echo, so the pre-unification normaliser could not match a single
        file in this farm's corpus. Real name pair from the production logs."""
        lf = await _add_library_file(db_session, "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id=None, library_file_id=lf.id)
        res = await resolve_terminal_item(
            db_session,
            1,
            {"subtask_id": "ECHOED-1", "subtask_name": "Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced"},
        )
        assert res.verdict == "matched_by_name"
        assert res.item.id == item.id

    async def test_spaced_library_name_rescues_underscored_echo(self, db_session):
        """The other half of the corpus: the library stores the SPACED display name and
        the printer echoes the underscored USB one."""
        lf = await _add_library_file(
            db_session, ".6 Half Shell_sharp_top_surfaces_painted_seams_Toprightv2.gcode_L1-88_spliced.3mf"
        )
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id=None, library_file_id=lf.id)
        res = await resolve_terminal_item(
            db_session,
            1,
            {
                "subtask_id": "ECHOED-2",
                "subtask_name": ".6_Half_Shell_sharp_top_surfaces_painted_seams_Toprightv2_L1-88_spliced",
            },
        )
        assert res.verdict == "matched_by_name"
        assert res.item.id == item.id

    async def test_near_miss_layer_range_does_not_attribute(self, db_session):
        """ATTRIBUTION SAFETY. Unifying the normaliser made this path MORE permissive
        (it gained space folding and ``.gcode``-token removal) — it did NOT make it
        fuzzy. Two spliced plates of the SAME project differing only by layer range are
        different prints, and ``matched_by_name`` mutates farm state, so a near miss
        must resolve foreign and leave the unit alone."""
        lf = await _add_library_file(db_session, "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id=None, library_file_id=lf.id)
        res = await resolve_terminal_item(
            db_session,
            1,
            {"subtask_id": "FOREIGN-9", "subtask_name": "Rotary_tool_top_surfaces_PCO-M12-2525_L1-91_spliced"},
        )
        assert res.verdict == "foreign"
        assert res.item is None
        await db_session.refresh(item)
        assert item.status == "printing"  # never handed over

    async def test_near_miss_never_crosses_between_two_farm_files(self, db_session):
        """Two DISTINCT library files whose keys differ by one real token: an echo
        naming the other one must not be credited to this unit."""
        lf = await _add_library_file(db_session, "Widget A.gcode_L1-90_spliced.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id=None, library_file_id=lf.id)
        res = await resolve_terminal_item(
            db_session, 1, {"subtask_id": "ECHOED-3", "subtask_name": "Widget_B_L1-90_spliced"}
        )
        assert res.verdict == "foreign"
        assert res.item is None
        await db_session.refresh(item)
        assert item.status == "printing"

    async def test_widened_key_cannot_reattribute_a_stamped_item(self, db_session):
        """The blast-radius BOUND that makes the widening acceptable: name matching
        applies ONLY to items with ``dispatch_subtask_id IS NULL``. A stamped item whose
        name now keys equal to the echo is still claimable by id equality alone — an
        operator re-printing the same file locally mints a fresh id (S4/S9)."""
        lf = await _add_library_file(db_session, "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf")
        item = await _add_item(db_session, printer_id=1, dispatch_subtask_id="SUB-1", library_file_id=lf.id)
        res = await resolve_terminal_item(
            db_session,
            1,
            {"subtask_id": "OTHER", "subtask_name": "Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced"},
        )
        assert res.verdict == "foreign"
        assert res.item is None
        await db_session.refresh(item)
        assert item.status == "printing"

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


class TestQueuePageStopCorrelation:
    """The QUEUE PAGE's Stop commits the row ``cancelled`` before the terminal arrives.

    Its terminal used to resolve FOREIGN (zero ``printing`` rows, an echoed id): no
    operator-stop hold, no fault requeue, and a foreign-plate page for the farm's own
    part. ``ui_stopped`` — the route's own mark, consumed at this terminal — lets the
    correlation owner match that row by its dispatch id.
    """

    async def _stopped(self, db, *, printer_id, subtask):
        item = await _add_item(db, printer_id=printer_id, status="cancelled", dispatch_subtask_id=subtask)
        item.stop_source = "operator_ui"
        item.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return item

    async def test_the_route_stopped_row_is_matched_by_its_dispatch_id(self, db_session):
        item = await self._stopped(db_session, printer_id=70, subtask="SUB-Q")

        res = await resolve_terminal_item(db_session, 70, {"subtask_id": "SUB-Q"}, ui_stopped=True)

        assert res.verdict == "matched"
        assert res.item.id == item.id

    async def test_without_the_routes_mark_it_is_still_foreign(self, db_session):
        """A later or duplicate terminal carries no mark (it was consumed at the first),
        so an already-handled row is never re-matched."""
        await self._stopped(db_session, printer_id=71, subtask="SUB-Q")

        res = await resolve_terminal_item(db_session, 71, {"subtask_id": "SUB-Q"}, ui_stopped=False)

        assert res.verdict == "foreign"

    async def test_a_different_id_is_still_foreign(self, db_session):
        await self._stopped(db_session, printer_id=72, subtask="SUB-Q")

        res = await resolve_terminal_item(db_session, 72, {"subtask_id": "SCREEN-9"}, ui_stopped=True)

        assert res.verdict == "foreign"

    async def test_a_row_cancelled_by_anything_but_the_operators_stop_is_not_matched(self, db_session):
        item = await _add_item(db_session, printer_id=73, status="cancelled", dispatch_subtask_id="SUB-Q")
        item.stop_source = "reconcile_unknown"
        await db_session.commit()

        res = await resolve_terminal_item(db_session, 73, {"subtask_id": "SUB-Q"}, ui_stopped=True)

        assert res.verdict == "foreign"


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


class TestFarmWorkSlatedFor:
    """The wider helper: farm work bound to a printer OR POOLED onto it.

    Consumed by the idle deep-park, which must not lower a bed the scheduler is
    about to land a unit on — pinned, model-targeted or printer-subset alike.
    Matching mirrors ``print_scheduler._find_idle_printer_for_target``: the item's
    target columns become a ``DispatchTarget`` and ``matches`` decides membership
    (a model target is normalised, then compared case-insensitively to the
    printer's model; a printers target is a bare id-set membership test).
    """

    async def test_true_for_bound_pending_item(self, db_session):
        # The bound half — delegated verbatim to farm_work_targets_printer.
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=5, status="pending", batch_id=batch.id)
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is True

    async def test_true_for_model_targeted_pending_item(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=None, status="pending", batch_id=batch.id, target_model="H2S")
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is True

    async def test_true_for_alias_spelling_and_case(self, db_session):
        # The scheduler normalises the item's target_model ("Bambu Lab H2S" → "H2S")
        # and compares case-insensitively against the stored Printer.model.
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=None, status="pending", batch_id=batch.id, target_model="Bambu Lab H2S")
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is True
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="h2s") is True

    async def test_true_for_printers_pool_member(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(
            db_session,
            printer_id=None,
            status="pending",
            batch_id=batch.id,
            target_printer_ids=encode_printer_ids([5, 9]),
        )
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is True

    async def test_false_for_printers_pool_non_member(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(
            db_session,
            printer_id=None,
            status="pending",
            batch_id=batch.id,
            target_printer_ids=encode_printer_ids([5, 9]),
        )
        # Same model as the members, but not IN the pool — a subset is a membership
        # test, never a model test.
        assert await farm_work_slated_for(db_session, printer_id=7, printer_model="H2S") is False

    async def test_false_for_printing_item(self, db_session):
        # Only PENDING work is slated; a printing item is already on a printer.
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=None, status="printing", batch_id=batch.id, target_model="H2S")
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is False

    async def test_false_for_non_farm_batch(self, db_session):
        plain = PrintBatch(name="plain")  # no sku_file_id → not a farm batch
        db_session.add(plain)
        await db_session.commit()
        await db_session.refresh(plain)
        await _add_item(db_session, printer_id=None, status="pending", batch_id=plain.id, target_model="H2S")
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is False

    async def test_false_for_a_different_model(self, db_session):
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=None, status="pending", batch_id=batch.id, target_model="P1S")
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="H2S") is False

    async def test_missing_printer_model_answers_by_target_kind(self, db_session):
        # An unidentified printer matches no MODEL target (an unknown model is not a
        # wildcard) — but a printer SUBSET names it by id, which needs no model at all.
        batch = await _add_farm_batch(db_session)
        await _add_item(db_session, printer_id=None, status="pending", batch_id=batch.id, target_model="H2S")
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model=None) is False
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model="  ") is False

        await _add_item(
            db_session,
            printer_id=None,
            status="pending",
            batch_id=batch.id,
            target_printer_ids=encode_printer_ids([5]),
        )
        assert await farm_work_slated_for(db_session, printer_id=5, printer_model=None) is True


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
        # A terminal carrying neither operator signal nor an unknown-outcome flag.
        assert classify_stop({"status": "aborted"}, 1, {2, 3}) is None

    def test_the_reconcile_flag_is_its_own_verdict(self):
        """The only verdict no ACTOR produced: the downtime reconcile could not learn
        the outcome, and an unknown outcome is not a completed one. Before it existed,
        such a terminal classified ``None`` and the disposition fork skipped it, so the
        run finished one plate short in silence."""
        assert classify_stop({"outcome_unknown": True}, 1, set()) == "reconcile_unknown"

    @pytest.mark.parametrize(
        "payload, membership, holds, expected",
        [
            ({"outcome_unknown": True}, {1}, (), "operator_ui"),
            ({"outcome_unknown": True, "user_cancel_observed": True}, set(), (), "operator_screen"),
            (
                {"outcome_unknown": True, "status": "aborted", "subtask_id": "JOB-1"},
                set(),
                ({"kind": "plate_vision", "job_id": "JOB-1"},),
                "plate_refused",
            ),
        ],
    )
    def test_the_unknown_ranks_below_every_real_signal(self, payload, membership, holds, expected):
        """It is what is LEFT when nothing speaks — a reconciled terminal that does
        carry a hold, an echo or a mark has a real cause, and that cause decides. (A
        paused plate check stopped while the farm was down reconciles as a refused
        plate: the reconcile echoes the job's own id.)"""
        assert classify_stop(payload, 1, membership, open_incidents=holds) == expected

    def test_a_false_flag_is_not_a_verdict(self):
        assert classify_stop({"outcome_unknown": False}, 1, set()) is None

    def test_false_echo_is_none(self):
        assert classify_stop({"user_cancel_observed": False}, 1, set()) is None


class TestClassifyStopPlateRefused:
    """``plate_refused``: a non-completed terminal of the job an open plate-check hold paused.

    The holds are the incident store's DB-free projection, captured BEFORE the terminal's
    own closers ran — the classifier never asks the store after the fact.
    """

    _HOLD = {"kind": "plate_vision", "job_id": "JOB-7", "printer_messages": []}

    @pytest.mark.parametrize("status", ["failed", "aborted", "cancelled"])
    def test_the_paused_jobs_non_completed_terminal_is_a_refused_plate(self, status):
        payload = {"status": status, "subtask_id": "JOB-7"}
        assert classify_stop(payload, 1, set(), open_incidents=[self._HOLD]) == STOP_VERDICT_PLATE_REFUSED

    def test_it_outranks_the_operators_ui_stop(self):
        """Pressing Stop on a paused plate check is how this verdict is USUALLY produced —
        what it means for the plate and the unit is the refusal, not the button."""
        payload = {"status": "failed", "subtask_id": "JOB-7"}
        assert classify_stop(payload, 1, {1}, open_incidents=[self._HOLD]) == STOP_VERDICT_PLATE_REFUSED

    def test_it_outranks_the_screen_echo(self):
        payload = {"status": "failed", "subtask_id": "JOB-7", "user_cancel_observed": True}
        assert classify_stop(payload, 1, set(), open_incidents=[self._HOLD]) == STOP_VERDICT_PLATE_REFUSED

    def test_a_completed_terminal_is_never_a_refusal(self):
        """The operator fixed the plate and resumed, and the job ran to the end — the
        hold was answered, and the part exists."""
        payload = {"status": "completed", "subtask_id": "JOB-7"}
        assert classify_stop(payload, 1, set(), open_incidents=[self._HOLD]) is None

    def test_another_jobs_terminal_is_not_this_holds_refusal(self):
        """The hold paused JOB-7; an eject sweep (or any other job) ending says nothing about it."""
        payload = {"status": "failed", "subtask_id": "EJECT-1"}
        assert classify_stop(payload, 1, {1}, open_incidents=[self._HOLD]) == "operator_ui"

    def test_a_fault_hold_of_the_same_job_is_not_a_refusal(self):
        """Only the plate-check kind refuses a plate; a runout hold of the same job is a
        fault, and the operator's stop over it keeps its own verdict."""
        runout = {"kind": "runout", "job_id": "JOB-7"}
        payload = {"status": "failed", "subtask_id": "JOB-7"}
        assert classify_stop(payload, 1, {1}, open_incidents=[runout]) == "operator_ui"

    def test_no_holds_is_the_pre_existing_behaviour(self):
        assert classify_stop({"status": "failed", "subtask_id": "JOB-7"}, 1, {1}) == "operator_ui"


class TestResolvePrintingFarmItem:
    """The ownership question, extracted from the deleted ``on_native_plate_detection``:
    is the farm loop responsible for what is on this printer?"""

    async def test_an_eject_profile_makes_it_a_farm_unit(self, db_session):
        item = await _add_eject_item(db_session, printer_id=55, eject_profile_id=7)
        assert (await resolve_printing_farm_item(db_session, 55)).id == item.id

    async def test_a_sku_batch_makes_it_a_farm_unit(self, db_session):
        batch = await _add_farm_batch(db_session)
        item = await _add_eject_item(db_session, printer_id=56, batch_id=batch.id)
        assert (await resolve_printing_farm_item(db_session, 56)).id == item.id

    async def test_a_plain_print_is_not_a_farm_unit(self, db_session):
        await _add_eject_item(db_session, printer_id=57)
        assert await resolve_printing_farm_item(db_session, 57) is None

    async def test_nothing_printing_is_none(self, db_session):
        assert await resolve_printing_farm_item(db_session, 58) is None


class TestPlateOccupancyCodeSet:
    """The vision codes the capture hook and the failure-reason attribution share."""

    def test_the_four_native_vision_codes_are_pinned_members(self):
        from backend.app.services.bambu_mqtt import _HMS_PLATE_OCCUPANCY_CODES

        assert {"0300_8017", "0300_8006", "0500_806E", "0500_808C"} <= _HMS_PLATE_OCCUPANCY_CODES


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


def _evidence(*, status: str = "completed") -> DepositEvidence:
    """A terminal that unambiguously deposited a part."""
    return DepositEvidence(
        final_status=status,
        is_dry_run=False,
        peaks_reliable=True,
        last_layer_num=120,
        last_progress=100.0,
    )


def _disposition(**overrides):
    """The happy shape: an id-confirmed farm unit with an eject profile."""
    kwargs = {
        "verdict": "matched",
        "item_id": 11,
        "eject_profile_id": 3,
        "first_article": False,
        "batch_id": 7,
        "source_subtask_id": "SUB-1",
        "evidence": _evidence(),
        "raise_gate": True,
    }
    kwargs.update(overrides)
    return terminal_disposition(**kwargs)


class TestTerminalDisposition:
    """The verdict → plate-policy ladder (WS3).

    The correlation rules live in this module, so the mapping from a VERDICT to
    "what happens to the plate next" lives here too — the occupancy authority stays
    free of the correlation lane and the terminal handler stays free of policy. The
    factory ASSEMBLES; every input is already resolved by the caller.
    """

    @pytest.mark.parametrize("verdict", ["matched", "matched_by_name"])
    def test_an_id_or_name_confirmed_farm_unit_arms_the_cooldown_sweep(self, verdict):
        disposition = _disposition(verdict=verdict)

        assert disposition.policy == CooldownEject(unit_id=11, run_id=7)

    def test_the_cooldown_policy_carries_a_null_run_for_an_unbatched_unit(self):
        assert _disposition(batch_id=None).policy == CooldownEject(unit_id=11, run_id=None)

    def test_fallback_never_arms_an_automatic_sweep(self):
        """``fallback`` is in ``ATTRIBUTED_VERDICTS`` but deliberately NOT in
        ``AUTO_CLEAR_VERDICTS``: a finish attributed only because it was the sole
        printing unit was never id-confirmed, so it may not sweep the plate."""
        assert isinstance(_disposition(verdict="fallback").policy, EscalationOnly)

    @pytest.mark.parametrize("verdict", ["foreign", "none"])
    def test_an_unattributed_terminal_escalates(self, verdict):
        """A print the farm did not dispatch (or could not attribute) lands on the
        never-armless floor. The foreign lane may UPGRADE this afterwards, once its
        background identification proves the plate is the farm's own file."""
        assert isinstance(_disposition(verdict=verdict, item_id=None).policy, EscalationOnly)

    def test_a_first_article_escalates_even_though_it_carries_a_profile(self):
        """The part holds on the plate for inspection; the approval flow arms its own
        FA eject."""
        assert isinstance(_disposition(first_article=True).policy, EscalationOnly)

    def test_a_unit_with_no_eject_profile_escalates(self):
        assert isinstance(_disposition(eject_profile_id=None).policy, EscalationOnly)

    def test_a_matched_verdict_with_no_item_escalates(self):
        assert isinstance(_disposition(item_id=None).policy, EscalationOnly)

    @pytest.mark.parametrize("raise_gate", [True, False])
    def test_raise_gate_is_carried_through_verbatim(self, raise_gate):
        """The caller's raise guard (the global ``require_plate_clear`` toggle, or
        farm involvement) rides ON the disposition, so the authority never has to
        know what that guard is made of — and a non-farm terminal on a toggle-off
        install still raises nothing."""
        assert _disposition(raise_gate=raise_gate).raise_gate is raise_gate

    def test_the_identity_and_the_evidence_ride_along_unchanged(self):
        evidence = _evidence(status="failed")

        disposition = _disposition(source_subtask_id="SUB-9", evidence=evidence)

        assert disposition.queue_item_id == 11
        assert disposition.source_subtask_id == "SUB-9"
        assert disposition.evidence is evidence

    def test_a_refused_plate_is_a_human_clear_hold_carrying_the_printers_words(self):
        """Tops the ladder: even an id-confirmed farm unit with an eject profile is NOT
        swept — the printer said the plate is wrong."""
        refusal = PlateRefusal(messages=(PrinterMessage(short_code="0500_808C", description="Detected offset."),))

        disposition = _disposition(refusal=refusal)

        assert disposition.policy == EscalationOnly(refusal=refusal)

    def test_a_refused_plate_raises_the_gate_whatever_the_toggle_says(self):
        """The toggle decides whether a finished job's DEPOSIT holds the plate; the printer
        refusing the plate is not an inference from a deposit."""
        refusal = PlateRefusal(messages=())
        assert _disposition(refusal=refusal, raise_gate=False).raise_gate is True

    def test_a_refused_plate_is_gated_sourceless(self):
        """Nothing was printed, so the refused job's id must not become the plate's sweep
        identity (the manual eject's matched lane would pair it with that unit)."""
        disposition = _disposition(refusal=PlateRefusal(messages=()), source_subtask_id="SUB-9")
        assert disposition.source_subtask_id is None


class TestUpgradeToForeignAutoEject:
    """The foreign lane's promotion, once its background identification lands."""

    def test_an_identified_foreign_plate_is_promoted_to_an_auto_sweep(self):
        """The foreign branch raises its gate SYNCHRONOUSLY under ``EscalationOnly``
        (a deposit must block dispatch NOW) and identifies the plate afterwards —
        that work opens archives and re-fetches donors over FTPS, which the terminal
        callback cannot wait on."""
        assert plate_occupancy.declare_occupied(21, Evidence()) is None

        assert upgrade_to_foreign_auto_eject(21, profile_id=4) is True

        assert plate_occupancy.snapshot(21).plate_policy == ForeignAutoEject(profile_id=4)

    def test_a_cleared_plate_refuses_the_promotion(self):
        """False means the authority refused ``not_occupied`` — an operator cleared
        the plate while we were identifying it, and an auto-eject onto a plate
        somebody already emptied is exactly what must not happen."""
        assert plate_occupancy.declare_occupied(22, Evidence()) is None
        assert plate_occupancy.clear_plate(22) is None

        assert upgrade_to_foreign_auto_eject(22, profile_id=4) is False

        assert plate_occupancy.snapshot(22).plate_policy is None

    def test_a_printer_that_never_had_a_plate_refuses_too(self):
        assert upgrade_to_foreign_auto_eject(23, profile_id=4) is False
        assert plate_occupancy.is_plate_occupied(23) is False
