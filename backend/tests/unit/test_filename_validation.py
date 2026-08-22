"""Validator tests for FAT32/exFAT-safe print filenames (#1540)."""

import pytest

from backend.app.utils.filename import (
    INVALID_FILENAME_CHARS,
    InvalidFilenameError,
    derive_remote_filename,
    print_identity_key,
    validate_print_filename,
)


class TestValidatePrintFilename:
    @pytest.mark.parametrize(
        "name",
        [
            "model.3mf",
            "Bersaglio.gcode.3mf",
            "Plate 1.3mf",
            "プリント.3mf",
            "model_v2-final.3mf",
            "a.3mf",
        ],
    )
    def test_valid_names_accepted(self, name: str) -> None:
        validate_print_filename(name)

    @pytest.mark.parametrize("char", list(INVALID_FILENAME_CHARS))
    def test_each_invalid_char_rejected(self, char: str) -> None:
        with pytest.raises(InvalidFilenameError) as exc_info:
            validate_print_filename(f"L{char}R.3mf")
        assert exc_info.value.char == char

    def test_pipe_from_issue_1540(self) -> None:
        """The exact reproducer from the bug report."""
        with pytest.raises(InvalidFilenameError) as exc_info:
            validate_print_filename("L|R.3mf")
        assert exc_info.value.char == "|"

    @pytest.mark.parametrize("name", ["", " ", "   "])
    def test_empty_rejected(self, name: str) -> None:
        with pytest.raises(InvalidFilenameError, match="empty"):
            validate_print_filename(name)

    @pytest.mark.parametrize("name", [".", ".."])
    def test_dot_names_rejected(self, name: str) -> None:
        with pytest.raises(InvalidFilenameError):
            validate_print_filename(name)

    def test_control_char_rejected(self) -> None:
        with pytest.raises(InvalidFilenameError, match="control"):
            validate_print_filename("file\x01.3mf")

    @pytest.mark.parametrize("name", ["file.3mf.", "file.3mf "])
    def test_trailing_space_or_dot_rejected(self, name: str) -> None:
        with pytest.raises(InvalidFilenameError, match="space or dot"):
            validate_print_filename(name)

    def test_too_long_rejected(self) -> None:
        with pytest.raises(InvalidFilenameError, match="bytes"):
            validate_print_filename("a" * 256)

    def test_unicode_byte_length_not_codepoint(self) -> None:
        """255 multi-byte codepoints exceeds 255 bytes — must reject."""
        # 'ä' is 2 bytes in UTF-8
        with pytest.raises(InvalidFilenameError, match="bytes"):
            validate_print_filename("ä" * 200)


class TestDeriveRemoteFilename:
    """SD-card upload-name derivation must match what the cleanup deletes (#1542)."""

    def test_strips_gcode_3mf(self) -> None:
        assert derive_remote_filename("Cube.gcode.3mf") == "Cube.3mf"

    def test_strips_3mf(self) -> None:
        assert derive_remote_filename("Cube.3mf") == "Cube.3mf"

    def test_bare_stem_appends_3mf(self) -> None:
        assert derive_remote_filename("Cube") == "Cube.3mf"

    def test_replaces_spaces_with_underscores(self) -> None:
        # firmware parses ftp://{filename} as a URL, spaces break it
        assert derive_remote_filename("Cube (1).gcode.3mf") == "Cube_(1).3mf"

    def test_doubled_gcode_3mf_fully_stripped(self) -> None:
        # The literal reproducer from #1542: library row had .gcode.3mf appended twice
        assert derive_remote_filename("Cube (1).gcode.3mf.gcode.3mf") == "Cube_(1).3mf"

    def test_doubled_3mf_fully_stripped(self) -> None:
        assert derive_remote_filename("Cube.3mf.3mf") == "Cube.3mf"

    def test_mixed_double_extensions_fully_stripped(self) -> None:
        assert derive_remote_filename("Cube.gcode.3mf.3mf") == "Cube.3mf"

    def test_raw_gcode_unchanged_stem(self) -> None:
        # Bare .gcode (no .3mf wrapper) is a valid sliced file — only the
        # .3mf wrapper gets stripped; .gcode survives and the result is
        # the printer's expected ftp:// target.
        assert derive_remote_filename("Cube.gcode") == "Cube.gcode.3mf"

    def test_idempotent(self) -> None:
        once = derive_remote_filename("Cube (1).gcode.3mf.gcode.3mf")
        assert derive_remote_filename(once) == once

    def test_unicode_stem_preserved(self) -> None:
        assert derive_remote_filename("プリント.gcode.3mf") == "プリント.3mf"

    def test_non_string_input_raises_typeerror(self) -> None:
        """A duck-typed object whose endswith always returns truthy must not be
        allowed to enter the strip loop — that's how a test mock OOM'd the
        container at 61 GB before the type guard was added."""
        from unittest.mock import MagicMock

        with pytest.raises(TypeError, match="requires str"):
            derive_remote_filename(MagicMock())
        with pytest.raises(TypeError, match="requires str"):
            derive_remote_filename(None)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="requires str"):
            derive_remote_filename(123)  # type: ignore[arg-type]


# Real production pairs (2026-08-22 logs): the library filename on disk, and the
# ``subtask_name`` the printer echoes back for that same print. The splicer writes a
# MID-STEM ``.gcode`` token and names plates with spaces; the firmware drops the token
# and underscores the spaces. Until these two keyed equal, the foreign-plate auto-eject
# rescue refused every plate this farm has ever printed.
SPLICED_CORPUS_PAIRS = [
    (
        "Rotary_tool_top_surfaces_PCO-M12-2525.gcode_L1-90_spliced.3mf",
        "Rotary_tool_top_surfaces_PCO-M12-2525_L1-90_spliced",
    ),
    (
        ".6 Half Shell_sharp_top_surfaces_painted_seams_Toprightv2.gcode_L1-88_spliced.3mf",
        ".6_Half_Shell_sharp_top_surfaces_painted_seams_Toprightv2_L1-88_spliced",
    ),
]


class TestPrintIdentityKey:
    """The ONE "is this the same print?" key — shared by terminal-status correlation
    and the foreign auto-eject identity check. Lossy on purpose; never names a file."""

    @pytest.mark.parametrize(("library_name", "echoed_name"), SPLICED_CORPUS_PAIRS)
    def test_production_pair_keys_equal(self, library_name: str, echoed_name: str) -> None:
        """The whole point: the on-disk name and the printer's echo of it are ONE print."""
        assert print_identity_key(library_name) == print_identity_key(echoed_name)

    def test_production_pair_keys_are_the_expected_value(self) -> None:
        # Pinned literally so a future "improvement" to the key cannot drift silently.
        assert print_identity_key(SPLICED_CORPUS_PAIRS[0][0]) == "rotary_tool_top_surfaces_pco-m12-2525_l1-90_spliced"
        assert (
            print_identity_key(SPLICED_CORPUS_PAIRS[1][0])
            == ".6_half_shell_sharp_top_surfaces_painted_seams_toprightv2_l1-88_spliced"
        )

    def test_name_without_gcode_token_is_just_stem_folded(self) -> None:
        """No ``.gcode`` anywhere — unchanged beyond suffix strip, fold and case."""
        assert print_identity_key("Cube.3mf") == "cube"
        assert print_identity_key("Cube") == "cube"
        assert print_identity_key("Widget_A_L1-5_spliced.3mf") == "widget_a_l1-5_spliced"

    def test_trailing_gcode_suffixes_stripped_repeatedly(self) -> None:
        assert print_identity_key("Cube.gcode.3mf") == "cube"
        assert print_identity_key("Cube.gcode") == "cube"
        assert print_identity_key("Cube.3mf.3mf") == "cube"

    def test_doubled_gcode_3mf_suffix_fully_stripped(self) -> None:
        # Same #1542 shape derive_remote_filename guards against.
        assert print_identity_key("Cube (1).gcode.3mf.gcode.3mf") == "cube_(1)"

    def test_space_containing_library_name_folds_to_underscores(self) -> None:
        """The library stores the SPACED display name; the USB/echo name is underscored."""
        assert print_identity_key("Widget A.3mf") == print_identity_key("Widget_A")
        assert print_identity_key(".6 nozzle (Battery holders X2).gcode.3mf") == ".6_nozzle_(battery_holders_x2)"

    def test_basename_stripped_from_a_path(self) -> None:
        assert print_identity_key("/data/Metadata/Widget A.3mf") == "widget_a"
        assert print_identity_key(r"C:\lib\Widget A.3mf") == "widget_a"

    def test_idempotent(self) -> None:
        once = print_identity_key(SPLICED_CORPUS_PAIRS[1][0])
        assert print_identity_key(once) == once

    def test_agrees_with_the_uploaded_usb_name(self) -> None:
        """The echo the printer returns is derived from what the uploader wrote, so the
        key must survive a round trip through derive_remote_filename. This is why the
        key folds spaces WITHOUT stripping surrounding whitespace."""
        for library_name, _ in SPLICED_CORPUS_PAIRS:
            usb_name = derive_remote_filename(library_name)
            assert print_identity_key(usb_name) == print_identity_key(library_name)

    def test_not_a_fuzzy_match(self) -> None:
        """It removes ONE known token — no edit distance, no prefix matching. Two names
        differing by any real token must key differently."""
        assert print_identity_key("Widget_A_L1-90_spliced.3mf") != print_identity_key("Widget_A_L1-91_spliced.3mf")
        assert print_identity_key("Widget_A.gcode_L1-90_spliced.3mf") != print_identity_key(
            "Widget_B.gcode_L1-90_spliced.3mf"
        )
        assert print_identity_key("Widget.3mf") != print_identity_key("Widget_2.3mf")

    def test_non_string_input_raises_typeerror(self) -> None:
        """Mirrors derive_remote_filename's guard: a duck-typed object whose endswith
        always returns truthy must never enter the strip loop (the 61 GB mock OOM)."""
        from unittest.mock import MagicMock

        with pytest.raises(TypeError, match="requires str"):
            print_identity_key(MagicMock())
        with pytest.raises(TypeError, match="requires str"):
            print_identity_key(None)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="requires str"):
            print_identity_key(123)  # type: ignore[arg-type]
