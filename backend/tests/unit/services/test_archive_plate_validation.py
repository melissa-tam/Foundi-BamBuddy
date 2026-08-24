"""Tests for the plate-validation helpers used by the print_start callback.

Covers the #1204 fix: when two plates of the same model are printed back to
back, MQTT subtask_name can lag and the FTP candidate built from it lands on
the previous plate's still-resident upload. The fix peeks the slice_info
plate index, compares it to the plate parsed from gcode_file, and (on
mismatch) re-fetches with a corrected name.

Pinned here:
- ``plate_indices_in_3mf`` reads ONLY ``Metadata/slice_info.config`` and returns
  EVERY declared plate index as a set, or an empty set on any failure (missing
  entry, malformed XML, unreadable zip, no ``index`` metadata). Cheap by design —
  the full parse runs later inside ArchiveService. It replaced
  ``peek_plate_index_in_3mf``, which answered with the FIRST ``<plate>`` block
  only: a valid proxy for containment on a single-plate Studio upload, and false
  for every multi-plate 3MF, which is what made the guard discard 57 correct files
  in the 30 days to 2026-08-23. An empty set means UNKNOWN, and callers must read
  it as "accept" — refusing on an unreadable answer discards the only gram source a
  tagless roll has.
- ``swap_plate_suffix`` rewrites the trailing plate number in a Bambu-style
  job name. Covers both the spaced "Project - Plate N" form and the
  underscored "project_plate_N" variant seen in real subtask_names.
"""

import zipfile

import pytest

from backend.app.services.archive import plate_indices_in_3mf, swap_plate_suffix


def _make_3mf(tmp_path, *, plate_index: int | None = None, malformed: bool = False):
    """Build a minimal 3MF with a single ``<plate>`` whose ``index`` metadata is set."""
    path = tmp_path / "test.3mf"
    if malformed:
        path.write_bytes(b"not a zip")
        return path
    with zipfile.ZipFile(path, "w") as zf:
        if plate_index is None:
            # plate present but with no index metadata — exercise the "no index" branch
            zf.writestr("Metadata/slice_info.config", "<config><plate></plate></config>")
        else:
            zf.writestr(
                "Metadata/slice_info.config",
                f'<config><plate><metadata key="index" value="{plate_index}" /></plate></config>',
            )
    return path


def _make_multi_plate_3mf(tmp_path, indices):
    """A 3MF declaring several plates, in the order given — the spliced-ladder shape."""
    path = tmp_path / "multi.3mf"
    blocks = "".join(f'<plate><metadata key="index" value="{i}" /></plate>' for i in indices)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", f"<config>{blocks}</config>")
    return path


class TestPlateIndicesIn3mf:
    def test_returns_the_index_for_a_single_plate_3mf(self, tmp_path):
        path = _make_3mf(tmp_path, plate_index=2)
        assert plate_indices_in_3mf(path) == {2}

    def test_returns_every_plate_of_a_multi_plate_3mf(self, tmp_path):
        """THE FIX: the first block is plate 1 and plate 3 is still in there.

        ``peek_plate_index_in_3mf`` answered 1 for this file, which is what made a
        printer running plate 3 look like a stale-name mismatch.
        """
        path = _make_multi_plate_3mf(tmp_path, [1, 2, 3, 4])
        assert plate_indices_in_3mf(path) == {1, 2, 3, 4}

    def test_returns_empty_when_index_missing(self, tmp_path):
        path = _make_3mf(tmp_path, plate_index=None)
        assert plate_indices_in_3mf(path) == set()

    def test_returns_empty_when_slice_info_absent(self, tmp_path):
        path = tmp_path / "noslice.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        assert plate_indices_in_3mf(path) == set()

    def test_returns_empty_on_non_zip_file(self, tmp_path):
        path = _make_3mf(tmp_path, malformed=True)
        assert plate_indices_in_3mf(path) == set()

    def test_returns_empty_on_missing_file(self, tmp_path):
        assert plate_indices_in_3mf(tmp_path / "does-not-exist.3mf") == set()

    def test_returns_empty_on_non_integer_index(self, tmp_path):
        path = tmp_path / "bad.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(
                "Metadata/slice_info.config",
                '<config><plate><metadata key="index" value="abc" /></plate></config>',
            )
        assert plate_indices_in_3mf(path) == set()

    def test_an_unparseable_index_does_not_hide_its_readable_siblings(self, tmp_path):
        """One malformed block is not a reason to call the whole file unreadable —
        the plate the printer is running may be one of the ones that DID parse."""
        path = tmp_path / "partial.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(
                "Metadata/slice_info.config",
                "<config>"
                '<plate><metadata key="index" value="abc" /></plate>'
                '<plate><metadata key="index" value="3" /></plate>'
                "</config>",
            )
        assert plate_indices_in_3mf(path) == {3}


class TestSwapPlateSuffix:
    @pytest.mark.parametrize(
        ("name", "target", "expected"),
        [
            # Bambu Studio's default form (spaces around hyphen, capitalised "Plate").
            ("MyModel - Plate 2", 1, "MyModel - Plate 1"),
            ("MyModel - Plate 1", 5, "MyModel - Plate 5"),
            # Hyphen variants without surrounding spaces should still match (regex
            # uses \s* — slicer output occasionally normalises spacing).
            ("Tight-Plate 3", 7, "Tight-Plate 7"),
            # Underscored form seen in real subtask_names (see
            # test_print_start_expected_promotion fixture "Box3.0_(2)_plate_5").
            ("Box3.0_(2)_plate_5", 1, "Box3.0_(2)_plate_1"),
            # Case-insensitive match — older exports occasionally use lowercase.
            ("model - plate 4", 2, "model - plate 2"),
        ],
    )
    def test_swaps_plate_number(self, name, target, expected):
        assert swap_plate_suffix(name, target) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "JustAModelName",  # No plate suffix at all — single-plate project.
            "Model with - Plate in middle of name",  # "Plate" not at the end.
            "Plate 2",  # Bare "Plate N" with no base — refuse rather than guess.
            "",  # Empty string.
        ],
    )
    def test_returns_none_when_no_recognised_suffix(self, name):
        assert swap_plate_suffix(name, 1) is None

    def test_returns_none_for_none_input(self):
        assert swap_plate_suffix(None, 1) is None

    def test_preserves_separator_casing(self):
        # The replacement must not normalise " - Plate " to "_plate_" or vice versa.
        # Otherwise the corrected name won't match what BambuStudio actually uploaded.
        assert swap_plate_suffix("Model - Plate 1", 2) == "Model - Plate 2"
        assert swap_plate_suffix("model_plate_1", 2) == "model_plate_2"
