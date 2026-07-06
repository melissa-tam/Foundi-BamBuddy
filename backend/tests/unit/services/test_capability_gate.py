"""Unit tests for the farm capability-matching dispatch gate (Phase 4).

The decision matrix is exercised against the PURE ``evaluate_capability`` (no DB,
no MQTT); the async ``check_dispatch_capability`` wrapper is tested for the
non-farm bypass and an end-to-end farm block/pass with a monkeypatched printer
state.
"""

from types import SimpleNamespace

import pytest

from backend.app.services import capability_gate as cg
from backend.app.services.capability_gate import (
    CapabilityDecision,
    FileCapabilities,
    evaluate_capability,
    extract_file_capabilities,
    live_nozzle_diameter,
    loaded_filament_types,
)

# A canonical "all-match" file: H2S, 0.6 nozzle, PETG.
_H2S_PETG = FileCapabilities(model="H2S", nozzle_diameter=0.6, filament_types=("PETG",))


class TestEvaluateCapabilityMatrix:
    def test_all_match_passes_without_warning(self):
        d = evaluate_capability(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True
        assert d.warn is False
        assert d.reason is None

    def test_model_mismatch_blocks(self):
        d = evaluate_capability(
            file_caps=FileCapabilities(model="X1C", nozzle_diameter=0.6, filament_types=("PETG",)),
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=["PETG"],
        )
        assert d.ok is False
        assert "sliced for" in d.reason

    def test_printer_without_bed_geometry_blocks(self):
        # X1C has no eject bed geometry (not a PRINTER_BED_DIMS key) → hard block.
        d = evaluate_capability(
            file_caps=FileCapabilities(model="X1C", nozzle_diameter=0.4, filament_types=("PLA",)),
            printer_model="X1C",
            live_nozzle_diameter="0.4",
            loaded_filament_types=["PLA"],
        )
        assert d.ok is False
        assert "no eject bed geometry" in d.reason

    def test_model_normalisation_slice_code_and_display_name_match(self):
        # File carries a slice code (O1S) / display name; printer stores "H2S".
        for file_model in ("O1S", "Bambu Lab H2S", "H2S"):
            d = evaluate_capability(
                file_caps=FileCapabilities(model=file_model, nozzle_diameter=0.6, filament_types=("PETG",)),
                printer_model="H2S",
                live_nozzle_diameter="0.6",
                loaded_filament_types=["PETG"],
            )
            assert d.ok is True, f"{file_model} should match H2S"

    def test_nozzle_mismatch_blocks(self):
        d = evaluate_capability(
            file_caps=_H2S_PETG,  # needs 0.6
            printer_model="H2S",
            live_nozzle_diameter="0.4",
            loaded_filament_types=["PETG"],
        )
        assert d.ok is False
        assert "nozzle mismatch" in d.reason

    def test_nozzle_unknown_warn_dispatches(self):
        # Printer reports no nozzle diameter → UNKNOWN → proceed with a warning.
        d = evaluate_capability(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzle_diameter=None,
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True
        assert d.warn is True
        assert "does not report nozzle" in d.reason

    def test_filament_mismatch_blocks(self):
        d = evaluate_capability(
            file_caps=_H2S_PETG,  # needs PETG
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=["PLA"],
        )
        assert d.ok is False
        assert "filament mismatch" in d.reason

    def test_filament_unknown_does_not_block(self):
        # AMS state unavailable (None) → cannot determine → proceed (no block).
        d = evaluate_capability(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=None,
        )
        assert d.ok is True

    def test_filament_empty_known_does_not_block(self):
        # Trays present but unspooled (known-empty) → proceed, don't block.
        d = evaluate_capability(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=[],
        )
        assert d.ok is True

    def test_filament_equivalence_group_matches(self):
        # File needs PA-CF; printer has PA12-CF (same equivalence group) → pass.
        d = evaluate_capability(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.6, filament_types=("PA-CF",)),
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=["PA12-CF"],
        )
        assert d.ok is True

    def test_filament_overlap_when_multiple_required(self):
        # File lists PETG,PLA; printer has only PLA → overlap → pass (not disjoint).
        d = evaluate_capability(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.6, filament_types=("PETG", "PLA")),
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=["PLA"],
        )
        assert d.ok is True

    def test_missing_file_facts_warn_but_pass(self):
        # File records neither model nor nozzle nor filament → warn-dispatch.
        d = evaluate_capability(
            file_caps=FileCapabilities(model=None, nozzle_diameter=None, filament_types=()),
            printer_model="H2S",
            live_nozzle_diameter="0.6",
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True
        assert d.warn is True
        assert "no sliced printer model" in d.reason
        assert "no nozzle diameter" in d.reason

    def test_nozzle_tolerance_allows_float_string_equality(self):
        d = evaluate_capability(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.4, filament_types=("PLA",)),
            printer_model="H2S",
            live_nozzle_diameter="0.40",
            loaded_filament_types=["PLA"],
        )
        assert d.ok is True


class TestExtractFileCapabilities:
    def test_full_metadata(self):
        caps = extract_file_capabilities(
            {"sliced_for_model": "H2S", "nozzle_diameter": 0.6, "filament_type": "PETG, PLA"}
        )
        assert caps.model == "H2S"
        assert caps.nozzle_diameter == 0.6
        assert caps.filament_types == ("PETG", "PLA")

    def test_falls_back_to_printer_model_id_key(self):
        caps = extract_file_capabilities({"printer_model_id": "O1S", "nozzle_diameter": "0.4"})
        assert caps.model == "O1S"
        assert caps.nozzle_diameter == 0.4

    def test_empty_or_none_metadata(self):
        for meta in (None, {}, {"unrelated": 1}):
            caps = extract_file_capabilities(meta)
            assert caps == FileCapabilities(model=None, nozzle_diameter=None, filament_types=())

    def test_filament_type_as_list(self):
        caps = extract_file_capabilities({"filament_type": ["PETG", "PLA"]})
        assert caps.filament_types == ("PETG", "PLA")


class TestReaders:
    def test_live_nozzle_diameter_reads_main_nozzle(self):
        status = SimpleNamespace(nozzles=[SimpleNamespace(nozzle_diameter="0.6")])
        assert live_nozzle_diameter(status) == "0.6"

    def test_live_nozzle_diameter_unknown(self):
        assert live_nozzle_diameter(None) is None
        assert live_nozzle_diameter(SimpleNamespace(nozzles=[])) is None
        assert live_nozzle_diameter(SimpleNamespace(nozzles=[SimpleNamespace(nozzle_diameter="")])) is None

    def test_loaded_filament_types_from_ams_and_vt_tray(self):
        status = SimpleNamespace(
            raw_data={
                "ams": [{"tray": [{"tray_type": "PETG"}, {"tray_type": ""}]}],
                "vt_tray": [{"tray_type": "PLA"}],
            }
        )
        assert sorted(loaded_filament_types(status)) == ["PETG", "PLA"]

    def test_loaded_filament_types_unknown_when_no_ams_structure(self):
        assert loaded_filament_types(SimpleNamespace(raw_data={})) is None
        assert loaded_filament_types(None) is None

    def test_loaded_filament_types_known_empty(self):
        # AMS present but trays unspooled → known-empty list, not None.
        status = SimpleNamespace(raw_data={"ams": [{"tray": [{"tray_type": ""}]}]})
        assert loaded_filament_types(status) == []


@pytest.mark.asyncio
class TestCheckDispatchCapability:
    async def test_non_farm_item_bypasses_gate(self):
        # batch_id is None → not a farm item → OK without touching DB/MQTT.
        item = SimpleNamespace(batch_id=None, library_file_id=None)
        printer = SimpleNamespace(id=1, model="X1C")
        d = await cg.check_dispatch_capability(db=None, item=item, printer=printer)
        assert d == CapabilityDecision(ok=True)

    async def test_plain_batch_without_sku_file_bypasses(self, db_session):
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem

        batch = PrintBatch(name="plain", quantity=1, status="active")  # no sku_file_id
        db_session.add(batch)
        await db_session.commit()
        await db_session.refresh(batch)
        item = PrintQueueItem(batch_id=batch.id, status="pending")
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        printer = SimpleNamespace(id=99, model="X1C")  # would fail the gate if run
        d = await cg.check_dispatch_capability(db=db_session, item=item, printer=printer)
        assert d.ok is True

    async def _make_farm_item(self, db_session, file_metadata):
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.sku import Sku, SkuFile

        lib = LibraryFile(
            filename="p.gcode.3mf",
            file_path="/tmp/p.gcode.3mf",
            file_type="gcode.3mf",
            file_size=1,
            is_external=True,
            file_metadata=file_metadata,
        )
        db_session.add(lib)
        sku = Sku(code="SKU-CAP", name="cap")
        db_session.add(sku)
        await db_session.commit()
        await db_session.refresh(lib)
        await db_session.refresh(sku)
        sf = SkuFile(sku_id=sku.id, library_file_id=lib.id, plate_index=1, units_per_plate=1)
        db_session.add(sf)
        await db_session.commit()
        await db_session.refresh(sf)
        batch = PrintBatch(name="run", quantity=1, status="active", sku_file_id=sf.id)
        db_session.add(batch)
        await db_session.commit()
        await db_session.refresh(batch)
        item = PrintQueueItem(batch_id=batch.id, library_file_id=lib.id, status="pending")
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)
        return item

    async def test_farm_item_blocks_on_nozzle_mismatch(self, db_session, monkeypatch):
        item = await self._make_farm_item(
            db_session, {"sliced_for_model": "H2S", "nozzle_diameter": 0.6, "filament_type": "PETG"}
        )
        status = SimpleNamespace(
            nozzles=[SimpleNamespace(nozzle_diameter="0.4")],
            raw_data={"ams": [{"tray": [{"tray_type": "PETG"}]}]},
        )
        monkeypatch.setattr(cg.printer_manager, "get_status", lambda pid: status)
        printer = SimpleNamespace(id=7, model="H2S")
        d = await cg.check_dispatch_capability(db=db_session, item=item, printer=printer)
        assert d.ok is False
        assert "nozzle mismatch" in d.reason

    async def test_farm_item_passes_when_all_match(self, db_session, monkeypatch):
        item = await self._make_farm_item(
            db_session, {"sliced_for_model": "H2S", "nozzle_diameter": 0.6, "filament_type": "PETG"}
        )
        status = SimpleNamespace(
            nozzles=[SimpleNamespace(nozzle_diameter="0.6")],
            raw_data={"ams": [{"tray": [{"tray_type": "PETG"}]}]},
        )
        monkeypatch.setattr(cg.printer_manager, "get_status", lambda pid: status)
        printer = SimpleNamespace(id=7, model="H2S")
        d = await cg.check_dispatch_capability(db=db_session, item=item, printer=printer)
        assert d.ok is True
