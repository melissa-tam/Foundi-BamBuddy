"""Unit tests for SKU auto-suggestion regex parsing (Phase 2)."""

from backend.app.services.sku_catalog import parse_sku_suggestion


class TestParseSkuSuggestion:
    def test_object_name_full_match(self):
        # The production corpus object name shape.
        res = parse_sku_suggestion(
            ["SKU007.01 M18 Hex Impact Driver (#2656-20).stl"],
            "_6_Half_Shell_v2_2656-20_top_surface_gcode.3mf",
        )
        assert res["code"] == "SKU007.01"
        assert res["part_number"] == "2656-20"
        assert res["name"] == "M18 Hex Impact Driver"
        assert res["matched_from"] == "object_name"

    def test_part_number_not_taken_from_sku_code_digits(self):
        # SKU007.01 has no 4-digit run; the part must come from "2656-20".
        res = parse_sku_suggestion(["SKU007.01 Widget (#2656-20).stl"], None)
        assert res["code"] == "SKU007.01"
        assert res["part_number"] == "2656-20"

    def test_hash_stripped_from_part_number(self):
        res = parse_sku_suggestion(["Thing (#3453).stl"], None)
        assert res["part_number"] == "3453"

    def test_part_number_without_hash_and_without_suffix(self):
        res = parse_sku_suggestion([], "battery_holder_2967.3mf")
        assert res["part_number"] == "2967"
        assert res["code"] is None
        assert res["matched_from"] == "filename"

    def test_falls_back_to_filename_for_code(self):
        res = parse_sku_suggestion(["a plain object.stl"], "SKU012.03_thing_1234.3mf")
        assert res["code"] == "SKU012.03"
        assert res["part_number"] == "1234"
        assert res["matched_from"] == "filename"

    def test_object_name_wins_over_filename(self):
        res = parse_sku_suggestion(
            ["SKU007.01 Real Part (#2656-20).stl"],
            "SKU999.99_9999.3mf",
        )
        assert res["code"] == "SKU007.01"
        assert res["matched_from"] == "object_name"

    def test_no_match_all_none(self):
        res = parse_sku_suggestion(["nondescript.stl"], "plain.3mf")
        assert res["code"] is None
        assert res["part_number"] is None
        assert res["matched_from"] is None

    def test_empty_inputs(self):
        res = parse_sku_suggestion([], None)
        assert res == {"code": None, "part_number": None, "name": None, "matched_from": None}

    def test_part_number_with_suffix_form(self):
        res = parse_sku_suggestion(["Gadget (#4820-10).stl"], None)
        assert res["part_number"] == "4820-10"

    def test_multi_unit_plate_name_cleaned(self):
        # "Battery holders X2, X3 & X6" style — first 4-digit run is the part.
        res = parse_sku_suggestion([], ".6 nozzle (Battery holders X2, X3 & X6, 3453 & 3404, 2967).3mf")
        assert res["part_number"] == "3453"
