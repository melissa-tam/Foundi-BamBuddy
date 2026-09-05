"""The requeue ALLOWLIST: every ``PrintQueueItem`` column has a decided home (W10).

Before ``queue_builder.requeue_fields`` there were two partial copies of "the same
settings" — ``farm_policy.create_retry_if_absent`` named 16 columns and
``production_run.top_up_run`` named 8 — so a retried or replacement plate silently
reverted 19 columns to model defaults: ``use_ams``, the whole calibration block,
``skip_filament_check``, the operator's slot pin, the timelapse flag. Nothing failed;
the plate just printed differently from the one it replaced.

The census test below is the guard that could not exist while the copies were
implicit: adding a column to the model without deciding which set it joins breaks CI
with a decision to make, instead of quietly joining the "reverted to default" group.
"""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.sku import Sku, SkuFile
from backend.app.services import farm_policy, production_run
from backend.app.services.dispatch_target import encode_printer_ids
from backend.app.services.queue_builder import (
    CARRIED_COLUMNS,
    LINEAGE_COLUMNS,
    NOT_CARRIED_COLUMNS,
    requeue_fields,
)

# Every CARRIED column, set to a value that is NOT the model's default, so a column
# that silently fails to travel is visible in the assertion rather than hidden behind
# a matching default. ``ams_mapping`` is here too: this row is built ``pending`` and
# PINNED, which is the one shape that carries the operator's slot pin.
_DISTINCTIVE = {
    "plate_id": 3,
    "project_id": 77,
    "print_time_seconds": 4321,
    "required_filament_types": '["PETG"]',
    "target_location": "shelf-2",
    "created_by_id": 42,
    "first_article": True,
    "is_dry_run": True,
    "use_ams": False,
    "ams_mapping": "[2,-1,-1,-1]",
    "filament_overrides": '[{"slot_id": 1, "type": "PETG", "color": "#000000"}]',
    "nozzle_mapping": "[1,0]",
    "skip_filament_check": True,
    "require_previous_success": True,
    "bed_levelling": False,
    "flow_cali": True,
    "vibration_cali": False,
    "layer_inspect": True,
    "timelapse": True,
    "nozzle_offset_cali": False,
    "gate_acknowledged": True,
    "auto_off_after": True,
    "gcode_injection": True,
}


async def _mk_farm_run(db, *, quantity=2):
    lib = LibraryFile(
        filename="f.gcode.3mf",
        file_path="/tmp/f.gcode.3mf",
        file_type="gcode.3mf",
        file_size=1,
        is_external=True,
        file_metadata={},
    )
    db.add(lib)
    await db.flush()
    sku = Sku(code=f"SKU{lib.id:03d}", name="Widget")
    db.add(sku)
    await db.flush()
    sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=1)
    db.add(sf)
    await db.flush()
    batch = PrintBatch(
        name="run",
        quantity=quantity,
        status="active",
        sku_file_id=sf.id,
        target_units=quantity,
        require_first_article=False,
        retry_max_per_unit=1,
        escalate_consecutive_failures=2,
    )
    db.add(batch)
    await db.flush()
    await db.commit()
    return batch, lib


async def _mk_item(db, batch, lib, **overrides):
    fields = {
        "batch_id": batch.id,
        "library_file_id": lib.id,
        "printer_id": 3,
        "status": "pending",
        "position": 1,
        **_DISTINCTIVE,
        **overrides,
    }
    item = PrintQueueItem(**fields)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


class TestColumnCensus:
    def test_every_column_belongs_to_exactly_one_set(self):
        """The union IS the model's column set — the pin that forces a decision.

        A new column has to join CARRIED (it is part of what the operator configured
        for this plate), NOT_CARRIED (it is per-attempt state, an identity/timestamp,
        or a target column ``DispatchTarget`` owns) or LINEAGE (the chain). There is
        no fourth answer, and "nobody thought about it" is no longer one of them.
        """
        columns = set(PrintQueueItem.__table__.columns.keys())
        union = CARRIED_COLUMNS | NOT_CARRIED_COLUMNS | LINEAGE_COLUMNS
        assert not union - columns, "the allowlist names a column the model does not have"
        assert not columns - union, "a PrintQueueItem column has no decided requeue home"

    def test_the_three_sets_are_disjoint(self):
        assert not CARRIED_COLUMNS & NOT_CARRIED_COLUMNS
        assert not CARRIED_COLUMNS & LINEAGE_COLUMNS
        assert not NOT_CARRIED_COLUMNS & LINEAGE_COLUMNS

    def test_the_target_columns_are_never_the_allowlist_s(self):
        """``DispatchTarget.fields()`` owns them, spread LAST by every minting path."""
        for column in ("printer_id", "target_model", "target_printer_ids"):
            assert column in NOT_CARRIED_COLUMNS


@pytest.mark.asyncio
class TestRoundTrip:
    async def test_every_carried_column_reaches_the_retry_row(self, db_session):
        batch, lib = await _mk_farm_run(db_session)
        item = await _mk_item(db_session, batch, lib, status="failed", waiting_reason="stale", error_message="boom")

        retry = await farm_policy.create_retry_if_absent(db_session, item)

        assert retry is not None
        for column in CARRIED_COLUMNS:
            expected = getattr(item, column)
            if column == "ams_mapping":
                continue  # its own contract — see TestAmsMappingContract
            assert getattr(retry, column) == expected, f"{column} did not survive the requeue"

    async def test_per_attempt_state_does_not_reach_the_retry_row(self, db_session):
        batch, lib = await _mk_farm_run(db_session)
        item = await _mk_item(
            db_session,
            batch,
            lib,
            status="failed",
            waiting_reason="printer_offline_stalled",
            error_message="HMS 0700_8011",
            dispatch_subtask_id="1802207420",
            stop_source="operator_ui",
            been_jumped=True,
            filament_short=True,
            cleanup_library_after_dispatch=True,
        )

        retry = await farm_policy.create_retry_if_absent(db_session, item)

        assert retry is not None
        assert retry.status == "pending"
        assert retry.waiting_reason is None
        assert retry.error_message is None
        assert retry.dispatch_subtask_id is None
        assert retry.stop_source is None
        assert retry.been_jumped is False
        assert retry.filament_short is False
        # A transient Direct-Print upload's delete-after-dispatch stamp must never be
        # copied: the first dispatch already reaped that file (2026-08-15 class).
        assert retry.cleanup_library_after_dispatch is False
        # Lineage is written by the requeue, never copied.
        assert retry.retry_of_id == item.id
        assert retry.retry_count == 1


@pytest.mark.asyncio
class TestAmsMappingContract:
    async def test_a_pending_pinned_row_carries_the_operator_pin(self, db_session):
        batch, lib = await _mk_farm_run(db_session)
        item = await _mk_item(db_session, batch, lib, status="pending", printer_id=3)
        assert requeue_fields(item)["ams_mapping"] == "[2,-1,-1,-1]"

    async def test_a_pending_pool_row_does_not(self, db_session):
        """A pin names trays on ONE printer; a pool unit's machine is not chosen yet."""
        batch, lib = await _mk_farm_run(db_session)
        model_row = await _mk_item(db_session, batch, lib, status="pending", printer_id=None, target_model="H2S")
        assert requeue_fields(model_row)["ams_mapping"] is None

        set_row = await _mk_item(
            db_session,
            batch,
            lib,
            status="pending",
            printer_id=None,
            target_printer_ids=encode_printer_ids([1, 3]),
            position=2,
        )
        assert requeue_fields(set_row)["ams_mapping"] is None

    async def test_a_dispatched_row_does_not_carry_the_decided_mapping(self, db_session):
        """Past ``pending`` the column is the DECIDED mapping, and the pin is GONE.

        ``claim_pending_for_dispatch`` overwrote the operator's pin with this
        dispatch's decision, so there is nothing to restore — carrying it forward
        would replay one printer's tray decision onto the next attempt.
        """
        batch, lib = await _mk_farm_run(db_session)
        for status in ("printing", "failed", "cancelled", "completed"):
            item = await _mk_item(db_session, batch, lib, status=status, printer_id=3, position=9)
            assert requeue_fields(item)["ams_mapping"] is None, status


@pytest.mark.asyncio
class TestBothConsumers:
    async def test_top_up_run_replacements_carry_the_settings(self, db_session):
        """The other consumer: RESUME's deficit top-up, which copied 8 columns."""
        batch, lib = await _mk_farm_run(db_session, quantity=2)
        template = await _mk_item(db_session, batch, lib, status="cancelled", first_article=False, position=1)
        await _mk_item(db_session, batch, lib, status="cancelled", first_article=False, position=2)

        run = await production_run._load_run(db_session, batch.id)
        created = await production_run.top_up_run(db_session, run)
        assert created == 2

        rows = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.batch_id == batch.id)))
            .scalars()
            .all()
        )
        replacements = [r for r in rows if r.status == "pending"]
        assert len(replacements) == 2
        for row in replacements:
            for column in CARRIED_COLUMNS:
                if column in ("ams_mapping", "first_article", "batch_id"):
                    continue  # own contracts: the pin, the forced False, the run id
                assert getattr(row, column) == getattr(template, column), f"{column} lost on top-up"
            # A replacement plate is never the run's first article.
            assert row.first_article is False
