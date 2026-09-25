"""Unit tests for services/plate_blowoff.py — the print-start auxiliary-fan plate blow-off.

The heads are the REAL Bambu Studio machine-start heads chute prime's goldens are cut
from (``fixtures/chute_prime/*.head.gcode``, pulled verbatim from the production corpus),
so every mutation below starts from a file a printer has run and changes exactly the one
thing its refusal is about. Heads are read in BINARY: the rewrite's promise is that it
inserts five lines and changes nothing else, and a test that decoded first could not see
a stray CR.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.services import chute_prime
from backend.app.services.plate_blowoff import (
    RECIPE_VERSION,
    AlreadyRewritten,
    Refused,
    Rewritten,
    insert_blowoff,
)

FIXTURES = Path(__file__).parent / "fixtures" / "chute_prime"

#: fixture stem → (1-based line of the ``;===== bed leveling`` banner, of ``G28 Z P0 T250``).
#: Spelled out rather than searched for: a test that found the anchor the way the module
#: does would pass on a module that had quietly started anchoring somewhere else.
CASES: dict[str, tuple[int, int]] = {
    "h2s_canonical": (855, 701),
    "h2s_m73": (855, 701),
    "h2c": (940, 718),
}

_BANNER = ";===== bed leveling =================================="

#: A short print body under the head, so the tests exercise a MEMBER (head + body), which
#: is what the dispatch seam hands the rewrite — not just the bounded head.
_BODY = "; LAYER_CHANGE\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F1800\nM400\n; EXECUTABLE_BLOCK_END\n"


def pulse(seconds: int = 10) -> list[str]:
    """The exact five lines the contract promises, independent of the module's constants."""
    return [
        f"; FARM PLATE BLOWOFF ({RECIPE_VERSION})",
        "M400",
        "M106 P2 S255",
        f"M400 S{seconds}",
        "M106 P2 S0",
    ]


def load_head(stem: str) -> bytes:
    return (FIXTURES / f"{stem}.head.gcode").read_bytes()


def member_of(stem: str) -> bytes:
    return load_head(stem) + _BODY.encode("utf-8")


def split_lines(data: bytes | str) -> list[str]:
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    return text.split("\n")


def join_lines(lines: list[str]) -> bytes:
    return "\n".join(lines).encode("utf-8")


def code_words(line: str) -> list[str]:
    body = line.lstrip()
    return [] if body.startswith(";") else body.partition(";")[0].split()


def window_of(lines: list[str]) -> range:
    start = next(i for i, ln in enumerate(lines) if ln.startswith("; EXECUTABLE_BLOCK_START"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith("; MACHINE_START_GCODE_END"))
    return range(start, end + 1)


def rewritten(stem: str, seconds: int = 10) -> Rewritten:
    outcome = insert_blowoff(member_of(stem), seconds=seconds)
    assert isinstance(outcome, Rewritten), f"{stem}: {outcome}"
    return outcome


def refused(lines: list[str]) -> Refused:
    outcome = insert_blowoff(join_lines(lines), seconds=10)
    assert isinstance(outcome, Refused), f"expected a refusal, got {outcome}"
    return outcome


class TestFixtureFacts:
    """The premises the placement rests on, read off the fixtures themselves."""

    @pytest.mark.parametrize("stem", CASES)
    def test_banner_and_z_home_are_where_the_module_docstring_says(self, stem: str):
        banner_line, z_home_line = CASES[stem]
        lines = split_lines(load_head(stem))
        assert lines[banner_line - 1] == _BANNER
        assert lines[z_home_line - 1] == "G28 Z P0 T250"

    @pytest.mark.parametrize("stem", CASES)
    def test_the_config_block_quotes_the_banner_and_the_heating_branch(self, stem: str):
        # The reason the search is windowed: every file quotes both, outside the window.
        lines = split_lines(load_head(stem))
        config = next(ln for ln in lines if ln.startswith("; machine_start_gcode = "))
        assert "bed leveling" in config
        assert "M145 P1" in config
        assert lines.index(config) < window_of(lines).start

    @pytest.mark.parametrize("stem", CASES)
    def test_z_switches_precede_the_banner_and_are_not_leveling(self, stem: str):
        # G29.1 / G29.2 sit above the banner on every fixture; only a bare G29 probes.
        banner_line, _ = CASES[stem]
        lines = split_lines(load_head(stem))
        above = [code_words(ln)[:1] for ln in lines[window_of(lines).start : banner_line - 1]]
        assert ["G29.2"] in above
        assert ["G29"] not in above


class TestInsertion:
    @pytest.mark.parametrize("stem", CASES)
    def test_the_pulse_is_inserted_immediately_before_the_banner(self, stem: str):
        banner_line, _ = CASES[stem]
        outcome = rewritten(stem)
        lines = split_lines(outcome.text)
        at = banner_line - 1
        assert outcome.anchor_line == banner_line
        assert lines[at : at + 5] == pulse()
        assert lines[at + 5] == _BANNER

    @pytest.mark.parametrize("stem", CASES)
    def test_the_pulse_follows_the_z_home_and_precedes_every_g29(self, stem: str):
        banner_line, z_home_line = CASES[stem]
        lines = split_lines(rewritten(stem).text)
        marker = lines.index(pulse()[0])
        g29 = [i for i in window_of(lines) if code_words(lines[i])[:1] == ["G29"]]
        assert z_home_line - 1 < marker
        assert g29, "the fixture's leveling section lost its G29"
        assert marker + 5 <= min(g29)
        assert marker == banner_line - 1

    @pytest.mark.parametrize("stem", CASES)
    def test_everything_else_is_byte_identical(self, stem: str):
        banner_line, _ = CASES[stem]
        before = member_of(stem)
        after = split_lines(rewritten(stem).text)
        del after[banner_line - 1 : banner_line + 4]
        assert join_lines(after) == before

    @pytest.mark.parametrize("seconds", [3, 10, 60])
    def test_the_dwell_is_the_seconds_handed_in(self, seconds: int):
        lines = split_lines(rewritten("h2s_canonical", seconds).text)
        at = CASES["h2s_canonical"][0] - 1
        assert lines[at : at + 5] == pulse(seconds)

    @pytest.mark.parametrize("stem", CASES)
    def test_the_dwell_is_m400_s_and_never_g4(self, stem: str):
        # G4 is invisible to the printer's time estimate; M400 S is the dialect's dwell.
        lines = split_lines(rewritten(stem).text)
        assert not any(code_words(ln)[:1] == ["G4"] for ln in lines)
        assert sum(code_words(ln) == ["M400", "S10"] for ln in lines) >= 1

    def test_bytes_and_text_input_give_the_same_member(self):
        as_bytes = insert_blowoff(member_of("h2c"), seconds=10)
        as_text = insert_blowoff(member_of("h2c").decode("utf-8"), seconds=10)
        assert as_bytes == as_text

    def test_a_crlf_member_gets_crlf_lines(self):
        crlf = member_of("h2s_canonical").replace(b"\n", b"\r\n")
        outcome = insert_blowoff(crlf, seconds=10)
        assert isinstance(outcome, Rewritten)
        lines = split_lines(outcome.text)
        at = CASES["h2s_canonical"][0] - 1
        assert lines[at : at + 5] == [f"{code}\r" for code in pulse()]
        del lines[at : at + 5]
        assert join_lines(lines) == crlf


class TestIdempotence:
    @pytest.mark.parametrize("stem", CASES)
    def test_second_pass_is_already_rewritten(self, stem: str):
        assert isinstance(insert_blowoff(rewritten(stem).text, seconds=10), AlreadyRewritten)

    def test_a_different_length_does_not_stack_a_second_pulse(self):
        # A donor archived from a printer carries the pulse it was built with; a later
        # dispatch at another length must not add a second one beside it.
        assert isinstance(insert_blowoff(rewritten("h2s_canonical", 10).text, seconds=30), AlreadyRewritten)


class TestStackWithChutePrime:
    """The dispatch order is chute prime → blow-off, each editing what it is handed."""

    @pytest.mark.parametrize("stem", CASES)
    def test_both_rewrites_apply_and_land_in_order(self, stem: str):
        banner_line, _ = CASES[stem]
        primed = chute_prime.rewrite_head(load_head(stem))
        assert isinstance(primed, chute_prime.Rewritten)

        outcome = insert_blowoff(primed.head, seconds=10)

        assert isinstance(outcome, Rewritten)
        # Chute prime edits only BELOW the banner, so the banner has not moved.
        assert outcome.anchor_line == banner_line
        lines = split_lines(outcome.text)
        blowoff_at = lines.index(pulse()[0])
        chute_at = next(i for i, ln in enumerate(lines) if "FARM CHUTE PRIME" in ln)
        assert blowoff_at < banner_line - 1 + 5 < chute_at
        assert lines[blowoff_at + 5] == _BANNER
        # Chute prime's own output survives intact: removing the pulse gives it back.
        del lines[blowoff_at : blowoff_at + 5]
        assert join_lines(lines) == primed.head


class TestRefusals:
    """One test per refusal reason, each a one-thing mutation of a real head."""

    def test_undecodable(self):
        outcome = insert_blowoff(b"; EXECUTABLE_BLOCK_START\n\xff\xfe\n; MACHINE_START_GCODE_END\n", seconds=10)
        assert isinstance(outcome, Refused) and outcome.reason == "undecodable"

    def test_no_start_block_without_the_end_marker(self):
        lines = [ln for ln in split_lines(member_of("h2s_canonical")) if not ln.startswith("; MACHINE_START_GCODE_END")]
        assert refused(lines).reason == "no_start_block"

    def test_no_start_block_without_the_start_marker(self):
        lines = [ln for ln in split_lines(member_of("h2s_canonical")) if not ln.startswith("; EXECUTABLE_BLOCK_START")]
        assert refused(lines).reason == "no_start_block"

    @pytest.mark.parametrize("stem", ["h2s_canonical", "h2c"])
    def test_anchor_missing(self, stem: str):
        lines = split_lines(member_of(stem))
        lines[CASES[stem][0] - 1] = ";===== levelling moved ====="
        assert refused(lines).reason == "anchor_missing"

    def test_the_closing_banner_is_never_the_anchor(self):
        # Delete the opening banner only: the ";===== bed leveling end" line is still there
        # and must not be mistaken for it.
        lines = split_lines(member_of("h2s_canonical"))
        del lines[CASES["h2s_canonical"][0] - 1]
        assert any(ln.startswith(";===== bed leveling end") for ln in lines)
        assert refused(lines).reason == "anchor_missing"

    @pytest.mark.parametrize("stem", ["h2s_canonical", "h2c"])
    def test_anchor_ambiguous(self, stem: str):
        lines = split_lines(member_of(stem))
        at = CASES[stem][0] - 1
        lines.insert(at, lines[at])
        refusal = refused(lines)
        assert refusal.reason == "anchor_ambiguous"
        assert "2 bed-leveling banners" in refusal.detail

    @pytest.mark.parametrize("stem", ["h2s_canonical", "h2c"])
    def test_no_z_home(self, stem: str):
        lines = split_lines(member_of(stem))
        _, z_home_line = CASES[stem]
        lines[z_home_line - 1] = "M400"
        assert refused(lines).reason == "no_z_home"

    def test_an_xy_only_home_is_not_a_z_home(self):
        # G28 X T300 stays in place; only the Z home is removed.
        lines = split_lines(member_of("h2s_canonical"))
        lines[CASES["h2s_canonical"][1] - 1] = "M400"
        assert "G28 X T300" in lines
        assert refused(lines).reason == "no_z_home"

    @pytest.mark.parametrize("stem", ["h2s_canonical", "h2c"])
    def test_leveling_before_anchor(self, stem: str):
        lines = split_lines(member_of(stem))
        at = CASES[stem][0] - 1
        lines.insert(at - 1, "G29 A1 O X145 Y135 I50 J50")
        refusal = refused(lines)
        assert refusal.reason == "leveling_before_anchor"
        assert f"line {at}" in refusal.detail

    @pytest.mark.parametrize("stem", ["h2s_canonical", "h2c"])
    def test_heating_airduct(self, stem: str):
        # The rendered heating branch: the vendor keeps the aux fan off in this mode.
        lines = split_lines(member_of(stem))
        at = next(i for i in window_of(lines) if code_words(lines[i]) == ["M145", "P0"])
        lines[at] = "    M145 P1 ; set airduct mode to heating mode for heating"
        assert refused(lines).reason == "heating_airduct"

    def test_the_last_selection_before_the_banner_is_the_one_in_force(self):
        lines = split_lines(member_of("h2s_canonical"))
        at = CASES["h2s_canonical"][0] - 1
        lines.insert(at, "M145 P1")
        assert refused(lines).reason == "heating_airduct"

    def test_airduct_unwitnessed_when_no_mode_is_selected(self):
        # M145.2 P0 F1 is left in place: it is a different command and must not be read
        # as the duct selection.
        lines = split_lines(member_of("h2s_canonical"))
        lines = [ln for ln in lines if code_words(ln)[:1] != ["M145"]]
        assert "M145.2 P0 F1" in lines
        refusal = refused(lines)
        assert refusal.reason == "airduct_unwitnessed"

    def test_airduct_unwitnessed_on_an_unknown_mode(self):
        lines = split_lines(member_of("h2c"))
        at = next(i for i in window_of(lines) if code_words(lines[i]) == ["M145", "P0"])
        lines[at] = "    M145 P7"
        assert refused(lines).reason == "airduct_unwitnessed"
