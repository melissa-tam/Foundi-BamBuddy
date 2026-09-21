"""``sku_catalog.plate_units`` — the one owner of "units one plate yields".

The rule was spelled four times: the SKU stats reducer, ``farm_policy``'s
run-completed notification and both ``production_run`` sites. Two of the four
had no floor below 1 (a negative row value in the reducer; the whole
``build_run_response`` arithmetic), so these pin the rule and the two consumer
behaviours the consolidation changes.
"""

import pytest

from backend.app.models.library import LibraryFile
from backend.app.models.print_batch import PrintBatch
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.sku import Sku, SkuFile
from backend.app.services.production_run import _load_run, build_run_response, plates_needed
from backend.app.services.sku_catalog import compute_stats_from_rows, plate_units


class TestPlateUnits:
    @pytest.mark.parametrize(
        "value,expected",
        [(12, 12), (3, 3), (1, 1), (0, 1), (-2, 1), (None, 1)],
    )
    def test_rule(self, value, expected):
        assert plate_units(value) == expected


class TestStatsReducer:
    def test_absent_key_counts_one(self):
        stats = compute_stats_from_rows([{"status": "completed"}])
        assert stats["units_completed"] == 1

    def test_negative_column_counts_one(self):
        # Changed behaviour: the old ``or 1`` passed a negative straight through,
        # so a bad row could SUBTRACT from a SKU's lifetime unit count.
        stats = compute_stats_from_rows([{"status": "completed", "units_per_plate": -4}])
        assert stats["units_completed"] == 1


class TestPlatesNeeded:
    @pytest.mark.parametrize("bad", [0, -1])
    def test_floors_units_per_plate_at_one(self, bad):
        assert plates_needed(4, bad) == 4


@pytest.mark.asyncio
class TestRunResponseUnitArithmetic:
    """``build_run_response`` was the spelling with NO floor at all."""

    async def _mk_run(self, db, *, units_per_plate: int) -> PrintBatch:
        lib = LibraryFile(filename="f.gcode.3mf", file_path="/tmp/f.gcode.3mf", file_type="gcode.3mf", file_size=1)
        db.add(lib)
        await db.flush()
        sku = Sku(code=f"SKU{lib.id:03d}", name="Widget")
        db.add(sku)
        await db.flush()
        sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=units_per_plate)
        db.add(sf)
        await db.flush()
        batch = PrintBatch(name="run", quantity=2, status="active", sku_file_id=sf.id, target_units=2)
        db.add(batch)
        await db.flush()
        db.add(PrintQueueItem(batch_id=batch.id, status="completed", plate_id=1, position=1))
        await db.commit()
        return batch

    async def test_zero_column_counts_one_unit_per_plate(self, db_session):
        # Previously ``plates_total * 0`` — a run reporting zero units planned,
        # zero completed, for plates it had actually produced.
        batch = await self._mk_run(db_session, units_per_plate=0)
        body = await build_run_response(db_session, await _load_run(db_session, batch.id))
        assert body["units_planned"] == 2
        assert body["units_completed"] == 1

    async def test_real_value_still_multiplies(self, db_session):
        batch = await self._mk_run(db_session, units_per_plate=4)
        body = await build_run_response(db_session, await _load_run(db_session, batch.id))
        assert body["units_planned"] == 8
        assert body["units_completed"] == 4
