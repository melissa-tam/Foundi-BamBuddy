"""Unit tests for the farm capability-matching dispatch gate (Phase 4).

The decision matrix is exercised against the PURE ``evaluate_capability`` (no DB,
no MQTT); the async ``check_dispatch_capability`` wrapper is tested for the
non-farm bypass and an end-to-end farm block/pass with a monkeypatched printer
state. The nozzle path is now dual-nozzle aware — per-extruder requirements from
``plate_capabilities`` plus the Vortek rack — while single-nozzle files keep the
exact legacy reason strings.
"""

from types import SimpleNamespace

import pytest

from backend.app.services import capability_gate as cg
from backend.app.services.capability_gate import (
    CapabilityDecision,
    FileCapabilities,
    LiveNozzles,
    NozzleRequirement,
    evaluate_capability,
    extract_file_capabilities,
    loaded_filament_types,
    read_live_nozzles,
)
from backend.app.utils.printer_models import extruder_for_ams, nozzle_for_ams_unit, side_label

# A canonical "all-match" file: H2S, 0.6 nozzle, PETG.
_H2S_PETG = FileCapabilities(model="H2S", nozzle_diameter=0.6, filament_types=("PETG",))


def _mounted(*diameters: str) -> LiveNozzles:
    """LiveNozzles with the given mounted-hotend diameters (index 0, 1, ...)."""
    return LiveNozzles(mounted=dict(enumerate(diameters)), rack={})


# The pure gate now takes the allowed (validated) model keys explicitly — the
# caller derives them from the geometry registry. Default the matrix to {"H2S"}.
def _eval(**kwargs):
    kwargs.setdefault("bed_dims_models", {"H2S"})
    return evaluate_capability(**kwargs)


class TestEvaluateCapabilityMatrix:
    def test_all_match_passes_without_warning(self):
        d = _eval(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True
        assert d.warn is False
        assert d.reason is None

    def test_model_mismatch_blocks(self):
        d = _eval(
            file_caps=FileCapabilities(model="X1C", nozzle_diameter=0.6, filament_types=("PETG",)),
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=["PETG"],
        )
        assert d.ok is False
        assert "sliced for" in d.reason

    def test_printer_without_bed_geometry_blocks(self):
        # X1C has no validated eject geometry (no registry row) → hard block.
        d = _eval(
            file_caps=FileCapabilities(model="X1C", nozzle_diameter=0.4, filament_types=("PLA",)),
            printer_model="X1C",
            live_nozzles=_mounted("0.4"),
            loaded_filament_types=["PLA"],
        )
        assert d.ok is False
        assert "no eject bed geometry" in d.reason

    def test_model_normalisation_slice_code_and_display_name_match(self):
        # File carries a slice code (O1S) / display name; printer stores "H2S".
        for file_model in ("O1S", "Bambu Lab H2S", "H2S"):
            d = _eval(
                file_caps=FileCapabilities(model=file_model, nozzle_diameter=0.6, filament_types=("PETG",)),
                printer_model="H2S",
                live_nozzles=_mounted("0.6"),
                loaded_filament_types=["PETG"],
            )
            assert d.ok is True, f"{file_model} should match H2S"

    def test_nozzle_mismatch_blocks(self):
        d = _eval(
            file_caps=_H2S_PETG,  # needs 0.6
            printer_model="H2S",
            live_nozzles=_mounted("0.4"),
            loaded_filament_types=["PETG"],
        )
        assert d.ok is False
        assert "nozzle mismatch" in d.reason

    def test_nozzle_unknown_warn_dispatches(self):
        # Printer reports no nozzle diameter → UNKNOWN → proceed with a warning.
        d = _eval(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzles=None,
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True
        assert d.warn is True
        assert "does not report nozzle" in d.reason

    def test_filament_mismatch_blocks(self):
        d = _eval(
            file_caps=_H2S_PETG,  # needs PETG
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=["PLA"],
        )
        assert d.ok is False
        assert "filament mismatch" in d.reason

    def test_filament_unknown_does_not_block(self):
        # AMS state unavailable (None) → cannot determine → proceed (no block).
        d = _eval(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=None,
        )
        assert d.ok is True

    def test_filament_empty_known_does_not_block(self):
        # Trays present but unspooled (known-empty) → proceed, don't block.
        d = _eval(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=[],
        )
        assert d.ok is True

    def test_filament_equivalence_group_matches(self):
        # File needs PA-CF; printer has PA12-CF (same equivalence group) → pass.
        d = _eval(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.6, filament_types=("PA-CF",)),
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=["PA12-CF"],
        )
        assert d.ok is True

    def test_filament_overlap_when_multiple_required(self):
        # File lists PETG,PLA; printer has only PLA → overlap → pass (not disjoint).
        d = _eval(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.6, filament_types=("PETG", "PLA")),
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=["PLA"],
        )
        assert d.ok is True

    def test_missing_file_facts_warn_but_pass(self):
        # File records neither model nor nozzle nor filament → warn-dispatch.
        d = _eval(
            file_caps=FileCapabilities(model=None, nozzle_diameter=None, filament_types=()),
            printer_model="H2S",
            live_nozzles=_mounted("0.6"),
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True
        assert d.warn is True
        assert "no sliced printer model" in d.reason
        assert "no nozzle diameter" in d.reason

    def test_nozzle_tolerance_allows_float_string_equality(self):
        d = _eval(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.4, filament_types=("PLA",)),
            printer_model="H2S",
            live_nozzles=_mounted("0.40"),
            loaded_filament_types=["PLA"],
        )
        assert d.ok is True


class TestNozzleDecisionTable:
    """The dual-nozzle-aware nozzle path (per-extruder requirements + Vortek rack).

    Single-nozzle arms assert the EXACT legacy reason strings (queue UI + the
    integration test depend on them byte-for-byte).
    """

    _LEGACY_MISMATCH = "Capability: nozzle mismatch — printer has 0.4mm, file needs 0.6mm"

    def test_single_nozzle_all_match(self):
        d = _eval(
            file_caps=_H2S_PETG, printer_model="H2S", live_nozzles=_mounted("0.6"), loaded_filament_types=["PETG"]
        )
        assert d.ok is True and d.warn is False

    def test_single_nozzle_mismatch_exact_legacy_reason(self):
        d = _eval(
            file_caps=_H2S_PETG, printer_model="H2S", live_nozzles=_mounted("0.4"), loaded_filament_types=["PETG"]
        )
        assert d.ok is False
        assert d.reason == self._LEGACY_MISMATCH

    def test_single_nozzle_unknown_warns(self):
        d = _eval(
            file_caps=_H2S_PETG,
            printer_model="H2S",
            live_nozzles=LiveNozzles(mounted={}, rack={}),
            loaded_filament_types=["PETG"],
        )
        assert d.ok is True and d.warn is True
        assert "printer does not report nozzle diameter (file needs 0.6mm)" in d.reason

    # --- dual, requirement pinned to an extruder ---
    def _dual_caps(self, requirements):
        return FileCapabilities(model="H2C", nozzle_diameter=None, filament_types=(), nozzle_requirements=requirements)

    def _dual(self, **kw):
        kw.setdefault("printer_model", "H2C")
        kw.setdefault("printer_is_dual", True)
        kw.setdefault("bed_dims_models", {"H2C"})
        kw.setdefault("loaded_filament_types", None)
        return evaluate_capability(**kw)

    def test_pinned_extruder_match(self):
        caps = self._dual_caps((NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=0),))
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.6", 1: "0.4"}, rack={}))
        assert d.ok is True and d.warn is False

    def test_pinned_extruder_mismatch_no_rack_blocks_with_side(self):
        caps = self._dual_caps((NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=0),))
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.4", 1: "0.4"}, rack={}))
        assert d.ok is False
        assert "nozzle mismatch" in d.reason
        assert "right" in d.reason  # side_label(0)
        assert "not in rack" in d.reason

    def test_pinned_extruder_mismatch_but_rack_has_it_ok(self):
        caps = self._dual_caps((NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=0),))
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.4"}, rack={16: "0.6"}))
        assert d.ok is True and d.warn is False

    def test_pinned_extruder_unknown_mounted_rack_match_ok(self):
        caps = self._dual_caps((NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=0),))
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={}, rack={16: "0.6"}))
        assert d.ok is True and d.warn is False

    def test_pinned_extruder_unknown_no_rack_warns_with_side(self):
        caps = self._dual_caps((NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=1),))
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={}, rack={}))
        assert d.ok is True and d.warn is True
        assert "left" in d.reason  # side_label(1)
        assert "does not report" in d.reason

    # --- dual, un-pinned scalar ---
    def test_dual_scalar_any_match_ok(self):
        caps = FileCapabilities(model="H2C", nozzle_diameter=0.6, filament_types=())
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.6", 1: "0.6"}, rack={}))
        assert d.ok is True and d.warn is False

    def test_dual_scalar_mixed_mounted_match_warns_differ(self):
        caps = FileCapabilities(model="H2C", nozzle_diameter=0.6, filament_types=())
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.4", 1: "0.6"}, rack={}))
        assert d.ok is True and d.warn is True
        assert "differ" in d.reason

    def test_dual_scalar_no_match_anywhere_blocks(self):
        caps = FileCapabilities(model="H2C", nozzle_diameter=0.6, filament_types=())
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.4", 1: "0.4"}, rack={}))
        assert d.ok is False
        assert "nozzle mismatch" in d.reason
        assert "file needs 0.6mm" in d.reason

    def test_h2s_corpus_regression_unpinned_requirement_is_legacy(self):
        # A single-nozzle file whose parser emitted a requirement with eid=None,
        # dispatched to a single-nozzle printer, must behave EXACTLY like the
        # legacy scalar path (same block reason).
        req_caps = FileCapabilities(
            model="H2S",
            nozzle_diameter=None,
            filament_types=("PETG",),
            nozzle_requirements=(NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=None),),
        )
        d = _eval(file_caps=req_caps, printer_model="H2S", live_nozzles=_mounted("0.4"), loaded_filament_types=["PETG"])
        assert d.ok is False
        assert d.reason == self._LEGACY_MISMATCH

    def test_requirement_missing_diameter_warns_not_blocks(self):
        caps = self._dual_caps((NozzleRequirement(slot_id=2, diameter=None, extruder_id=0),))
        d = self._dual(file_caps=caps, live_nozzles=LiveNozzles(mounted={0: "0.4"}, rack={}))
        assert d.ok is True and d.warn is True
        assert "records no nozzle diameter" in d.reason

    def test_arbitrary_diameters_no_hardcoding(self):
        # Prove the gate compares whatever the file/printer report — not "0.4".
        ok = _eval(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.8, filament_types=()),
            printer_model="H2S",
            live_nozzles=_mounted("0.8"),
            loaded_filament_types=None,
        )
        assert ok.ok is True
        bad = _eval(
            file_caps=FileCapabilities(model="H2S", nozzle_diameter=0.2, filament_types=()),
            printer_model="H2S",
            live_nozzles=_mounted("0.8"),
            loaded_filament_types=None,
        )
        assert bad.ok is False
        assert "printer has 0.8mm, file needs 0.2mm" in bad.reason


class TestExtractFileCapabilities:
    def test_full_metadata(self):
        caps = extract_file_capabilities(
            {"sliced_for_model": "H2S", "nozzle_diameter": 0.6, "filament_type": "PETG, PLA"}
        )
        assert caps.model == "H2S"
        assert caps.nozzle_diameter == 0.6
        assert caps.filament_types == ("PETG", "PLA")
        assert caps.nozzle_requirements == ()

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

    def test_plate_capabilities_exact_plate_pick(self):
        meta = {
            "plate_capabilities": {
                "1": {"filament_nozzles": [{"slot_id": 1, "nozzle_diameter": 0.4, "extruder_id": 1}]},
                "2": {"filament_nozzles": [{"slot_id": 3, "nozzle_diameter": 0.6, "extruder_id": 0}]},
            }
        }
        caps = extract_file_capabilities(meta, plate_id=2)
        assert caps.nozzle_requirements == (NozzleRequirement(slot_id=3, diameter=0.6, extruder_id=0),)

    def test_plate_capabilities_sole_entry_fallback(self):
        # plate_id does not match a key but there is exactly one entry → use it.
        meta = {"plate_capabilities": {"1": {"filament_nozzles": [{"slot_id": 1, "nozzle_diameter": 0.6}]}}}
        caps = extract_file_capabilities(meta, plate_id=7)
        assert caps.nozzle_requirements == (NozzleRequirement(slot_id=1, diameter=0.6, extruder_id=None),)

    def test_plate_capabilities_no_match_multi_entry_yields_nothing(self):
        meta = {
            "plate_capabilities": {
                "1": {"filament_nozzles": [{"nozzle_diameter": 0.4}]},
                "2": {"filament_nozzles": [{"nozzle_diameter": 0.6}]},
            }
        }
        caps = extract_file_capabilities(meta, plate_id=9)
        assert caps.nozzle_requirements == ()

    def test_legacy_scalar_only(self):
        caps = extract_file_capabilities({"nozzle_diameter": 0.4}, plate_id=1)
        assert caps.nozzle_requirements == ()
        assert caps.nozzle_diameter == 0.4

    def test_malformed_plate_capabilities_falls_back_to_scalar(self):
        caps = extract_file_capabilities({"nozzle_diameter": 0.4, "plate_capabilities": "garbage"})
        assert caps.nozzle_requirements == ()
        assert caps.nozzle_diameter == 0.4

    def test_malformed_filament_nozzles_entries_skipped(self):
        meta = {
            "plate_capabilities": {
                "1": {"filament_nozzles": ["not-a-dict", {"nozzle_diameter": "bad", "extruder_id": "x"}]}
            }
        }
        caps = extract_file_capabilities(meta, plate_id=1)
        # Second entry survives with None fields; the string entry is skipped.
        assert caps.nozzle_requirements == (NozzleRequirement(slot_id=None, diameter=None, extruder_id=None),)


class TestReadLiveNozzles:
    def test_none_status_returns_none(self):
        assert read_live_nozzles(None) is None

    def test_two_slot_with_empty_second_drops_empty(self):
        status = SimpleNamespace(nozzles=[SimpleNamespace(nozzle_diameter="0.6"), SimpleNamespace(nozzle_diameter="")])
        live = read_live_nozzles(status)
        assert live.mounted == {0: "0.6"}
        assert live.rack == {}

    def test_rack_only_ids_at_or_above_min_with_nonempty_diameter(self):
        # ids 0,1 are hotend echoes; 16,17,21 are rack slots; 17 empty → dropped.
        status = SimpleNamespace(
            nozzles=[SimpleNamespace(nozzle_diameter="0.6"), SimpleNamespace(nozzle_diameter="0.4")],
            nozzle_rack=[
                {"id": 0, "diameter": "0.6"},
                {"id": 1, "diameter": "0.4"},
                {"id": 16, "diameter": "0.6"},
                {"id": 17, "diameter": ""},
                {"id": 21, "diameter": "0.4"},
            ],
        )
        live = read_live_nozzles(status)
        assert live.mounted == {0: "0.6", 1: "0.4"}
        assert live.rack == {16: "0.6", 21: "0.4"}

    def test_rack_accepts_nozzle_diameter_key(self):
        # API-shaped entries use "nozzle_diameter"; the raw state uses "diameter".
        status = SimpleNamespace(nozzle_rack=[{"id": 16, "nozzle_diameter": "0.6"}])
        live = read_live_nozzles(status)
        assert live.rack == {16: "0.6"}

    def test_short_or_absent_lists_safe(self):
        live = read_live_nozzles(SimpleNamespace())  # no nozzles / nozzle_rack attrs
        assert live == LiveNozzles(mounted={}, rack={})
        live2 = read_live_nozzles(SimpleNamespace(nozzles=[], nozzle_rack=["not-a-dict"]))
        assert live2 == LiveNozzles(mounted={}, rack={})


class TestReaders:
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


class TestPrinterModelNozzleHelpers:
    """``side_label`` / ``extruder_for_ams`` / ``nozzle_for_ams_unit`` (utils)."""

    def test_side_label(self):
        assert side_label(0) == "right"
        assert side_label(1) == "left"
        assert side_label(None) == "?"
        assert side_label(5) == "?"

    def test_extruder_for_ams_external_spool(self):
        # Virtual AMS 255: tray 0 → extruder 1 (left), tray 1 → extruder 0 (right).
        assert extruder_for_ams(None, 255, tray_id=0) == 1
        assert extruder_for_ams(None, 255, tray_id=1) == 0
        assert extruder_for_ams(None, 255, tray_id=None) is None

    def test_extruder_for_ams_mapped_and_unknown(self):
        m = {"0": 0, "1": 1}
        assert extruder_for_ams(m, 0) == 0
        assert extruder_for_ams(m, 1) == 1
        assert extruder_for_ams(m, 2) is None  # key absent
        assert extruder_for_ams(None, 0) is None
        assert extruder_for_ams({}, 0) is None

    def test_nozzle_for_ams_unit_resolves_serving_extruder(self):
        state = SimpleNamespace(
            ams_extruder_map={"0": 0},
            nozzles=[SimpleNamespace(nozzle_diameter="0.6"), SimpleNamespace(nozzle_diameter="0.4")],
        )
        assert nozzle_for_ams_unit(state, 0) == "0.6"  # ams 0 → extruder 0 → 0.6
        assert nozzle_for_ams_unit(state, 255, tray_id=0) == "0.4"  # → extruder 1 → 0.4
        assert nozzle_for_ams_unit(state, 255, tray_id=1) == "0.6"  # → extruder 0 → 0.6

    def test_nozzle_for_ams_unit_falls_back_to_extruder_zero_when_unresolved(self):
        state = SimpleNamespace(
            ams_extruder_map={"0": 0},
            nozzles=[SimpleNamespace(nozzle_diameter="0.6"), SimpleNamespace(nozzle_diameter="0.4")],
        )
        assert nozzle_for_ams_unit(state, 9) == "0.6"  # ams 9 unmapped → extruder 0

    def test_nozzle_for_ams_unit_defaults_when_state_or_diameter_missing(self):
        assert nozzle_for_ams_unit(None, 0) == "0.4"
        empty = SimpleNamespace(ams_extruder_map={"0": 0}, nozzles=[SimpleNamespace(nozzle_diameter="")])
        assert nozzle_for_ams_unit(empty, 0) == "0.4"
        assert nozzle_for_ams_unit(empty, 0, default="0.6") == "0.6"


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

    async def _make_farm_item(self, db_session, file_metadata, *, model_key="H2S"):
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_batch import PrintBatch
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_model_geometry import PrinterModelGeometry
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
        db_session.add(
            PrinterModelGeometry(
                model_key=model_key,
                bed_x=340,
                bed_y=320,
                env_x_min=0,
                env_x_max=340,
                env_y_min=-16,
                env_y_max=325,
                max_part_height_mm=42,
                validated=True,
            )
        )
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
        item = PrintQueueItem(batch_id=batch.id, library_file_id=lib.id, status="pending", plate_id=1)
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

    async def test_farm_item_dual_printer_blocks_on_pinned_extruder(self, db_session, monkeypatch):
        # H2C file pins 0.6 to extruder 0; both hotends are 0.4 and the rack is
        # empty → BLOCK naming the "right" hotend.
        item = await self._make_farm_item(
            db_session,
            {
                "plate_capabilities": {
                    "1": {"filament_nozzles": [{"slot_id": 1, "nozzle_diameter": 0.6, "extruder_id": 0}]}
                }
            },
            model_key="H2C",
        )
        status = SimpleNamespace(
            nozzles=[SimpleNamespace(nozzle_diameter="0.4"), SimpleNamespace(nozzle_diameter="0.4")],
            raw_data={},
        )
        monkeypatch.setattr(cg.printer_manager, "get_status", lambda pid: status)
        printer = SimpleNamespace(id=8, model="H2C")
        d = await cg.check_dispatch_capability(db=db_session, item=item, printer=printer)
        assert d.ok is False
        assert "nozzle mismatch" in d.reason
        assert "right" in d.reason
