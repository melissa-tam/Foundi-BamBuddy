"""Unit tests for services/chute_prime.py — the plate-lip → purge-chute prime rewrite.

The fixtures under ``fixtures/chute_prime/`` are REAL Bambu Studio heads pulled verbatim
out of the production corpus (``foundi-FarmManager/Print Files``) with
``threemf_tools.read_plate_gcode_start_block``, and the ``*.expected_section.gcode``
goldens beside them are the full rewritten sections a human has read. They are read in
BINARY and compared byte-for-byte: the rewrite's whole promise is that it changes the
section and nothing else, and a test that decoded first could not see a stray CR.

Three heads, chosen because they are the three shapes the corpus actually contains:

* ``h2s_canonical`` — ``auto print test cube.gcode.3mf`` plate 1, the plain H2S section;
* ``h2s_m73`` — the battery-holder plate 5, where the slicer injected an UNINDENTED
  ``M73 P1 R291`` into the middle of the lip-visit group;
* ``h2c`` — ``print cube h2c.gcode.3mf`` plate 1, the dual-nozzle dialect carrying the
  record-and-persist group, blank lines and a second temperature command.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.services.chute_prime import (
    RECIPE_VERSION,
    AlreadyRewritten,
    Refused,
    Rewritten,
    rewrite_head,
)

FIXTURES = Path(__file__).parent / "fixtures" / "chute_prime"

#: fixture stem → the ΣE the slicer's own prime lines carry (H2S 5+20, H2C 5+10).
CASES: tuple[tuple[str, float], ...] = (
    ("h2s_canonical", 25.0),
    ("h2s_m73", 25.0),
    ("h2c", 15.0),
)

_OPEN_PREFIX = ";===== nozzle load line"
_CLOSE_TEXT = "load line end"

#: The line shapes the rewrite DROPS, spelled out here rather than imported from the
#: module: a refusal test that reused the module's own table would pass on a table that
#: had quietly stopped matching the corpus.
_LIP_VISIT_SHAPES: tuple[str, ...] = (
    r"G1\s+Z[-+\d.]+\s+F[\d.]+",
    r"G1\s+X[-+\d.]+\s+Y[-+\d.]+\s+F[\d.]+",
    r"G1\s+X[-+\d.]+\s+F[\d.]+",
    r"G3\s+Z[-+\d.]+\s+I[-+\d.]+\s+J[-+\d.]+\s+P[\d.]+\s+F[\d.]+",
    r"G28\.14\s+R[\d.]+",
    r"G29\.2\s+S0",
    r"G91",
)


def load_head(stem: str) -> bytes:
    return (FIXTURES / f"{stem}.head.gcode").read_bytes()


def load_golden(stem: str) -> bytes:
    return (FIXTURES / f"{stem}.expected_section.gcode").read_bytes()


def split_lines(head: bytes) -> list[str]:
    return head.decode("utf-8").split("\n")


def join_lines(lines: list[str]) -> bytes:
    return "\n".join(lines).encode("utf-8")


def open_index(lines: list[str]) -> int:
    return next(i for i, ln in enumerate(lines) if ln.lstrip().startswith(_OPEN_PREFIX) and _CLOSE_TEXT not in ln)


def close_index(lines: list[str]) -> int:
    start = open_index(lines)
    return next(i for i, ln in enumerate(lines) if i > start and ln.lstrip().startswith(";=====") and _CLOSE_TEXT in ln)


def section_of(head: bytes) -> bytes:
    """The marker-to-marker section, trailing newline included — the golden's shape."""
    lines = split_lines(head)
    return join_lines(lines[open_index(lines) : close_index(lines) + 1]) + b"\n"


def code_part(line: str) -> str:
    body = line.lstrip()
    return body if body.startswith(";") else body.partition(";")[0].rstrip()


def rewritten(stem: str) -> Rewritten:
    outcome = rewrite_head(load_head(stem))
    assert isinstance(outcome, Rewritten), f"{stem}: {outcome}"
    return outcome


class TestGoldenSections:
    """The whole rewritten section, byte-for-byte, against a golden a human has read."""

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_section_matches_golden(self, stem: str):
        assert section_of(rewritten(stem).head) == load_golden(stem)

    def test_recipe_version_is_stamped_in_the_marker(self):
        # The cache key rides RECIPE_VERSION; the emitted comment carries the same
        # generation, so a file on disk can be told apart from one built by a later recipe.
        assert RECIPE_VERSION == "chute-prime-v1"
        assert "FARM CHUTE PRIME v1" in section_of(rewritten("h2s_canonical").head).decode()


class TestOutsideTheSectionIsUntouched:
    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_bytes_outside_the_section_are_identical(self, stem: str):
        head = load_head(stem)
        before = split_lines(head)
        after = split_lines(rewritten(stem).head)
        assert before[: open_index(before)] == after[: open_index(after)]
        assert before[close_index(before) + 1 :] == after[close_index(after) + 1 :]

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_config_block_copy_of_the_marker_is_untouched(self, stem: str):
        # The CONFIG_BLOCK quotes the whole machine-start G-code unexpanded, marker text
        # and all, on one ``; machine_start_gcode = …`` line ABOVE the executable block.
        # It is a settings record, not code: rewriting it would corrupt the archive's
        # provenance and, on a reslice, the operator's own profile.
        def config_line(head: bytes) -> str:
            return next(ln for ln in split_lines(head) if ln.startswith("; machine_start_gcode = "))

        original = config_line(load_head(stem))
        assert _OPEN_PREFIX in original, "fixture no longer carries the CONFIG_BLOCK trap"
        assert config_line(rewritten(stem).head) == original

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_line_endings_and_trailing_newline_survive(self, stem: str):
        head = load_head(stem)
        assert b"\r" not in head, "fixture is not LF-only"
        out = rewritten(stem).head
        assert b"\r" not in out
        assert out.endswith(b"\n")
        assert head.endswith(b"\n")


class TestPrimeIsConserved:
    @pytest.mark.parametrize(("stem", "expected_mm"), CASES)
    def test_sigma_e_conserved_and_reported(self, stem: str, expected_mm: float):
        def sigma_e(section: bytes) -> float:
            total = 0.0
            for line in section.decode().split("\n"):
                m = re.fullmatch(r"G1(?:\s+X[-+\d.]+)?\s+E([\d.]+)\s+F[\d.]+", code_part(line))
                if m:
                    total += float(m.group(1))
            return total

        out = rewritten(stem)
        assert sigma_e(section_of(load_head(stem))) == expected_mm
        assert sigma_e(section_of(out.head)) == expected_mm
        assert out.prime_mm == expected_mm


class TestSectionInvariants:
    """What must be true of every rewritten section, whatever the dialect."""

    @staticmethod
    def _section_lines(stem: str) -> list[str]:
        return section_of(rewritten(stem).head).decode().split("\n")

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_lip_visit_group_is_gone(self, stem: str):
        codes = [code_part(ln) for ln in self._section_lines(stem)]
        assert "G91" not in codes, "relative mode left in force"
        assert not any(c.startswith("G28.14") for c in codes), "pre-extrude Z recall survived"
        assert not any(re.fullmatch(r"G29\.2\s+S0", c) for c in codes), "Z compensation left off"
        assert not any(c.startswith("G3 ") for c in codes), "peel-off arc survived"

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_no_xyz_word_outside_the_replayed_wiper_exit(self, stem: str):
        lines = self._section_lines(stem)
        # The exit is the four lines the emitter replays immediately after G150.1.
        wipe = next(i for i, ln in enumerate(lines) if code_part(ln) == "G150.1")
        exit_block = set(range(wipe + 1, wipe + 5))
        for i, line in enumerate(lines):
            code = code_part(line)
            if i in exit_block or not code or code.startswith(";"):
                continue
            offenders = [w for w in code.split() if w[:1] in ("X", "Y", "Z")]
            assert not offenders, f"{stem} line {i}: stray positioning word {offenders} in {line!r}"

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_modal_state_at_the_close_marker(self, stem: str):
        lines = self._section_lines(stem)
        codes = [code_part(ln) for ln in lines[:-2] if code_part(ln) and not code_part(ln).startswith(";")]
        assert codes[-3:] == ["G90", "M83", "G29.2 S1"]

    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_chute_macros_bracket_the_prime(self, stem: str):
        codes = [code_part(ln) for ln in self._section_lines(stem)]
        park, wipe = codes.index("G150.3"), codes.index("G150.1")
        first_prime = next(i for i, c in enumerate(codes) if re.fullmatch(r"G1\s+E[\d.]+\s+F[\d.]+", c))
        assert park < first_prime < wipe, "the prime must extrude between the park and the wipe"
        assert codes[wipe - 1] == "M400", "the wipe must wait for the prime to finish"


class TestIdempotence:
    @pytest.mark.parametrize("stem", [s for s, _ in CASES])
    def test_second_pass_is_already_rewritten(self, stem: str):
        # Reachable in production: foreign_archive archives the printer-resident — already
        # rewritten — copy of a screen-restarted job, which can come back round as a donor.
        assert isinstance(rewrite_head(rewritten(stem).head), AlreadyRewritten)


class TestClassifierOrdering:
    def test_prime_traverse_is_never_read_as_a_lip_visit_move(self):
        # ``G1 X290 E20 F623.623`` and ``G1 X250 F60000`` differ by one word. If the table
        # were ordered the other way the traverse would be DROPPED — the rewrite would
        # silently delete the very prime it exists to relocate, and the nozzle would reach
        # layer 1 de-pressurised. So: the traverse survives, the bare X move does not.
        section = section_of(rewritten("h2s_canonical").head).decode()
        codes = [code_part(ln) for ln in section.split("\n")]
        assert "G1 E20 F623.623" in codes, "the prime traverse was dropped"
        assert not any(re.fullmatch(r"G1\s+X[\d.]+\s+F[\d.]+", c) for c in codes)

    def test_retract_inside_the_section_refuses_rather_than_replays(self):
        # No row matches a negative E: a retract is not a prime, and guessing is worse
        # than shipping the file unmodified.
        lines = split_lines(load_head("h2s_canonical"))
        lines[open_index(lines) + 1 : open_index(lines) + 1] = ["    G1 E-3 F623.623"]
        outcome = rewrite_head(join_lines(lines))
        assert isinstance(outcome, Refused) and outcome.reason == "unknown_line"
        assert "E-3" in outcome.detail


class TestCloseMarkerSpellings:
    @pytest.mark.parametrize("spelling", ["noozle", "nozzle"])
    def test_both_spellings_close_the_section(self, spelling: str):
        # Bambu misspells the close marker as "noozle". Neither spelling may be assumed:
        # accepting only the typo breaks the day they fix it, and vice versa.
        lines = split_lines(load_head("h2s_canonical"))
        ci = close_index(lines)
        lines[ci] = f";===== {spelling} load line end ==========================="
        outcome = rewrite_head(join_lines(lines))
        assert isinstance(outcome, Rewritten)
        assert outcome.prime_mm == 25.0


class TestRefusals:
    """One test per refusal reason. Every mutation starts from a REAL head and changes
    exactly the one thing the reason is about, so a refusal can never be an artifact of a
    hand-written stub that was never a Bambu file."""

    @staticmethod
    def _refused(head: bytes) -> Refused:
        outcome = rewrite_head(head)
        assert isinstance(outcome, Refused), f"expected a refusal, got {outcome}"
        return outcome

    def test_undecodable(self):
        assert self._refused(b"; EXECUTABLE_BLOCK_START\n\xff\xfe not utf-8\n").reason == "undecodable"

    def test_no_start_block(self):
        lines = [ln for ln in split_lines(load_head("h2s_canonical")) if not ln.startswith("; EXECUTABLE_BLOCK_START")]
        assert self._refused(join_lines(lines)).reason == "no_start_block"

    def test_section_missing(self):
        lines = split_lines(load_head("h2s_canonical"))
        del lines[open_index(lines) : close_index(lines) + 1]
        assert self._refused(join_lines(lines)).reason == "section_missing"

    def test_section_ambiguous_on_a_second_open_marker(self):
        lines = split_lines(load_head("h2s_canonical"))
        oi = open_index(lines)
        lines.insert(oi, lines[oi])
        refusal = self._refused(join_lines(lines))
        assert refusal.reason == "section_ambiguous"
        assert "2 open" in refusal.detail

    def test_no_lip_visit_to_anchor_the_park(self):
        # Every drop dropped and nothing left to anchor G150.3 on: priming wherever the
        # toolhead happens to stand is the accident this module exists to prevent. The
        # refusal has its OWN word — folding it into ``section_ambiguous`` said "the
        # markers are wrong" about a section whose markers are perfect.
        lines = split_lines(load_head("h2s_canonical"))
        oi, ci = open_index(lines), close_index(lines)
        body = lines[oi + 1 : ci]
        lines[oi + 1 : ci] = [ln for ln in body if not any(re.fullmatch(p, code_part(ln)) for p in _LIP_VISIT_SHAPES)]
        # The prime itself must have survived the mutation, or this would be testing the
        # wrong refusal.
        assert any(code_part(ln).startswith("G1 X290 E20") for ln in lines)
        refusal = self._refused(join_lines(lines))
        assert refusal.reason == "no_lip_visit"
        assert "lip-visit" in refusal.detail

    def test_no_lip_visit_when_the_first_drop_follows_the_prime(self):
        # The park is emitted where the FIRST dropped line stood. Leave only the peel-off
        # arc — which sits BEHIND the prime — and the emitted order would be prime, then
        # G150.3: the bead lands wherever the toolhead stands, and the whole point of the
        # rewrite is inverted. Lip-visit lines exist here, so the "no drops at all" arm
        # cannot be what refuses this.
        lines = split_lines(load_head("h2s_canonical"))
        oi = open_index(lines)
        prime = next(i for i, ln in enumerate(lines) if i > oi and code_part(ln).startswith("G1 E5"))
        head_of_body = lines[oi + 1 : prime]
        lines[oi + 1 : prime] = [
            ln for ln in head_of_body if not any(re.fullmatch(p, code_part(ln)) for p in _LIP_VISIT_SHAPES)
        ]
        # The peel-off arc after the prime is still there — this is an ORDER refusal.
        assert any(code_part(ln).startswith("G3 Z0.4") for ln in lines)
        refusal = self._refused(join_lines(lines))
        assert refusal.reason == "no_lip_visit"
        assert "follows the first prime line" in refusal.detail

    def test_unknown_line(self):
        lines = split_lines(load_head("h2s_canonical"))
        lines.insert(open_index(lines) + 1, "    M9999 Q42 ; a shape no row knows")
        refusal = self._refused(join_lines(lines))
        assert refusal.reason == "unknown_line"
        assert refusal.detail == "    M9999 Q42 ; a shape no row knows"

    def test_no_prime_extrusion(self):
        lines = split_lines(load_head("h2s_canonical"))
        lines = [ln for ln in lines if not re.fullmatch(r"G1\s+X[\d.]+\s+E[\d.]+\s+F[\d.]+", code_part(ln))]
        refusal = self._refused(join_lines(lines))
        assert refusal.reason == "no_prime_extrusion"
        assert "found 0" in refusal.detail

    def test_no_temperature_wait(self):
        lines = split_lines(load_head("h2s_canonical"))
        oi, ci = open_index(lines), close_index(lines)
        lines[oi:ci] = [ln for ln in lines[oi:ci] if not code_part(ln).startswith("M109")]
        assert self._refused(join_lines(lines)).reason == "no_temperature_wait"

    def test_macros_unwitnessed(self):
        # A head that never used G150.1 has not shown us the macro is in its dialect, so
        # the rewrite may not emit it. Non-H2 files fail here or at the table.
        lines = split_lines(load_head("h2s_canonical"))
        oi = open_index(lines)
        lines[:oi] = [ln if code_part(ln).split()[:1] != ["G150.1"] else "M400" for ln in lines[:oi]]
        refusal = self._refused(join_lines(lines))
        assert refusal.reason == "macros_unwitnessed"
        assert "G150.1" in refusal.detail

    def test_wiper_exit_unrecognised(self):
        lines = split_lines(load_head("h2s_canonical"))
        oi = open_index(lines)
        last_code = max(i for i in range(oi) if lines[i].strip())
        assert code_part(lines[last_code]) == "G1 Y265 F18000", "fixture's exit block moved"
        lines[last_code] = "M400"
        assert self._refused(join_lines(lines)).reason == "wiper_exit_unrecognised"
