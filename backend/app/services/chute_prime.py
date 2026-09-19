"""Relocate Bambu Studio's start-block prime bead from the plate lip into the purge chute.

Every ``*.gcode.3mf`` the farm prints is sliced by Bambu Studio, whose H2-series
machine-start block ends with a section the slicer labels ``nozzle load line``: a
stationary blob plus a bead laid 0.8 mm tall on the plate's FRONT LIP. The farm's eject
sweep pushes parts rear → front with a Z floor of 0.4 mm, so that welded ridge sits under
the sweep and straight across the part's exit path — usually the part scrapes it off,
sometimes the part catches and the toolhead stalls against it.

This module is the pure half of the fix: :func:`rewrite_head` takes the plate G-code's
machine-start head and returns the same bytes with the prime relocated to the chute, or a
closed refusal. It has no DB handle, no I/O and no logging — the seam that calls it owns
the settings read, the cache and the WARNING that a refusal deserves.

**Why the prime must survive at all.** The line's only job is to re-pressurise the melt
zone: layer 1 opens ``G1 E-.8`` → travel → ``G1 E.8``, i.e. the slicer assumes a full
nozzle. BambuLoop (same machine, same sweep concept) hit the same residue and tried
dropping the prime outright; it failed on first-layer lag because the nozzle ended
DE-pressurised. So the prime is MOVED, never deleted, and no retract is added after it.

Group verdicts, with the file:line evidence they rest on (absolute lines in
``Metadata/plate_1.gcode`` of ``auto print test cube.gcode.3mf`` [H2S] and
``print cube h2c.gcode.3mf`` [H2C]):

* **LIP-VISIT group — DROPPED as a unit.** The approach (``G1 Z… F…``, ``G1 X… Y… F…``,
  ``G28.14 R0``, ``G29.2 S0``, ``G91``, ``G1 X… F…``) and the peel-off
  (``G3 Z… I… J… P… F…``) exist only to put the nozzle 0.8 mm above the lip and lift it
  off again. ``G28.14 R0`` is the RECALL of a Z calibrated OUTSIDE this section
  (``G28.140 S0 ; cali pre-extrude z pos`` — H2S :896, H2C :982); its only consumer is
  this group's own relative ``G1 Z0.8``, so recalling it produces nothing to persist. The
  ``G90`` lines interleaved with the group are STATE and stay, so absolute mode is never
  lost and ``G91`` is never left in force. The group is also the ANCHOR: the chute park
  is emitted at the FIRST dropped line's position, so that line must exist and must come
  BEFORE the first prime line — a section ordered the other way would park after priming,
  i.e. prime wherever the toolhead happens to stand, which is the accident this module
  exists to prevent. Either shape refuses ``no_lip_visit``.
* **RECORD-AND-PERSIST group — KEPT in place, in order** (``========== record data ==========``,
  ``M1026``, ``M1012.8``, ``M1012.9``, ``G29.9``, ``M400 P…``, ``M500 D1``, ``M400 S…``;
  witnessed on H2C only). Running them at the chute is not a new idea: the vendor itself
  runs the same record pair AT THE CHUTE on H2S — ``G150.3 ; move to garbage can to wait
  for temp`` :925 / ``M1026`` :926 / ``G29.9`` :927 — so the group keeps the machine state
  it was written against. ``M500 D1`` persists records the dropped group never wrote.
* **STATE lines — KEPT verbatim** (``G29.2 S1``, ``G90``, ``M83``, ``M104 S…``,
  ``M109 S…``, ``M400``, ``M73 P… R…``). ``M73`` is injected mid-section by the slicer at
  an unindented column and must survive exactly where it sits.
* **PRIME — KEPT, with the traverse made stationary.** The one ``G1 X… E… F…`` traverse is
  re-emitted with ONLY its ``X`` word removed; its ``E`` and ``F`` words are the file's
  own bytes. Every number this module emits is replayed from the file — no temperature, E
  amount, feedrate or coordinate is a literal here (project red line 3).

**Emission.** ``G150.3`` (travel to the garbage can) takes the place of the lip-visit
group's FIRST line, so the ``M109`` wait and H2C's ``M400 S3`` ooze into the chute rather
than onto the plate. After the last prime line: ``M400``, ``G150.1`` (nozzle wipe), then
the vendor's own wiper-exit block replayed from the four code lines immediately above the
open marker. The section's own tail (``G90`` / ``M83`` / ``G29.2 S1``) closes it. Stated
cost: the stock ``G150.1`` + exit above the marker still run, so the wipe/exit pair
executes twice — one extra toolhead round trip per print.

**Self-evidencing dialect gate.** The bare ``G150.3`` / ``G150.1`` forms are emitted only
when the same head already used both (``macros_unwitnessed`` otherwise), and the replayed
exit must match its shape (``wiper_exit_unrecognised``). Non-H2 files — X1/P1 draw their
calibration line on the bed with a different vocabulary — fail the classifier table and
ship unmodified. That is the whole safety argument: this module never invents a motion, it
only deletes, reorders and replays motions the file itself contains.

**Fail-closed by construction.** The classifier is an ORDERED TABLE with one row per
witnessed dialect line shape, and a line no row matches refuses the whole file
(``unknown_line``) rather than being passed through unclassified. A retract (``E-…``)
inside the section has no row for exactly this reason.

Two traps this module is built around, both live in the corpus:

* the close marker is MISSPELLED by Bambu as ``noozle load line end`` — both spellings are
  accepted, and neither is assumed;
* the same marker text appears again, unexpanded, inside the CONFIG_BLOCK line
  ``; machine_start_gcode = …`` (H2S :315, H2C :312 — BEFORE ``; EXECUTABLE_BLOCK_START``
  at :578 / :575). The search window is ``; EXECUTABLE_BLOCK_START`` …
  ``; MACHINE_START_GCODE_END`` for that reason alone, and the CONFIG_BLOCK copy is never
  touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

#: Identifies the recipe these bytes were produced by. The dispatch seam folds it into the
#: derived-artifact cache key, so bumping it re-derives every cached file. Bump it whenever
#: the emitted section changes shape — a cache hit on the previous recipe is otherwise
#: indistinguishable from a hit on this one.
RECIPE_VERSION = "chute-prime-v1"

#: The substring that makes a rewritten section self-identifying. Matched anywhere inside
#: the section, so the marker comment's wording may evolve without breaking idempotence.
#: This is reachable in production: ``foreign_archive`` archives the printer-resident —
#: already rewritten — copy of a screen-restarted job, which can come back round as a donor.
_MARKER_TOKEN = "FARM CHUTE PRIME"

_MARKER_COMMENT = f"; {_MARKER_TOKEN} v1: prime relocated from the plate lip to the purge chute"

#: Vendor tool motions, emitted bare and only once the same head has witnessed both forms.
#: ``G150.3`` travels to the garbage can / purge chute, ``G150.1`` wipes the nozzle mouth.
_CHUTE_PARK = "G150.3"
_CHUTE_WIPE = "G150.1"

#: Emitted between the last prime line and the wipe: the prime must be complete before the
#: toolhead moves, or the bead trails out of the chute.
_SYNC = "M400"

_EXEC_BLOCK_START_MARKER = "; EXECUTABLE_BLOCK_START"
_START_GCODE_END_MARKER = "; MACHINE_START_GCODE_END"

_SECTION_OPEN_PREFIX = ";===== nozzle load line"
_SECTION_CLOSE_PREFIX = ";====="
_SECTION_CLOSE_SPELLINGS = ("noozle load line end", "nozzle load line end")

#: A signed G-code number. Bambu emits both ``-0.5`` and bare-dot forms, so neither the
#: leading digit nor the sign may be assumed.
_NUM = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"

#: An UNSIGNED G-code number. The prime rows use this rather than :data:`_NUM` so a retract
#: (``E-3``) matches no row and refuses the file instead of being replayed as a prime.
_POS = r"\+?(?:\d+(?:\.\d+)?|\.\d+)"

#: What the emitter does with a classified line.
#:
#: ``keep`` — re-emitted verbatim, byte for byte including indentation and any comment.
#: ``drop`` — deleted; the FIRST dropped line's position is where the chute park is emitted.
#: ``prime_stationary`` — re-emitted verbatim; its E counts toward ``prime_mm``.
#: ``prime_traverse`` — re-emitted with only its ``X`` word removed.
_Disposition = Literal["keep", "drop", "prime_stationary", "prime_traverse"]

#: Why a head could not be rewritten. Closed on purpose: a tenth way for this to go wrong
#: has to be added here, where the seam's WARNING and the test-per-reason can see it,
#: rather than appearing as a bare ``return None`` in the middle of the walk.
ChutePrimeRefusal = Literal[
    "undecodable",  # the head is not strict UTF-8
    "no_start_block",  # no ; EXECUTABLE_BLOCK_START … ; MACHINE_START_GCODE_END window
    "section_missing",  # the window carries no nozzle-load-line markers at all
    "section_ambiguous",  # markers present but not exactly one ordered pair
    "unknown_line",  # a section line no classifier row matches
    "no_prime_extrusion",  # not exactly one prime traverse to relocate
    "no_temperature_wait",  # nothing waits for print temperature before the prime
    "no_lip_visit",  # no dropped lip-visit line BEFORE the prime to anchor the chute park on
    "macros_unwitnessed",  # this head never used G150.3 / G150.1, so we may not emit them
    "wiper_exit_unrecognised",  # the four lines above the marker are not the vendor's exit
]


@dataclass(frozen=True)
class Rewritten:
    """The head with the prime relocated into the chute.

    ``head`` is a drop-in replacement for the bytes handed in: everything outside the
    nozzle-load-line section is byte-identical, line endings and the trailing newline are
    preserved, and the caller may splice it back over the member's prefix. ``prime_mm`` is
    the total E extruded by the prime lines, unchanged by the rewrite — the invariant a
    caller logs and a test pins.
    """

    head: bytes
    prime_mm: float


@dataclass(frozen=True)
class AlreadyRewritten:
    """This head carries our marker comment already — nothing to do, and not a problem."""


@dataclass(frozen=True)
class Refused:
    """The head was left alone. ``detail`` is for the log line, not for branching on."""

    reason: ChutePrimeRefusal
    detail: str


ChutePrimeOutcome: TypeAlias = Rewritten | AlreadyRewritten | Refused


@dataclass(frozen=True)
class _Row:
    """One dialect line shape and what becomes of it.

    ``name`` is the evidence handle used in refusal details and asserted by the table
    tests; ``pattern`` is full-matched against the line's executable part.
    """

    name: str
    pattern: re.Pattern[str]
    disposition: _Disposition


def _row(name: str, pattern: str, disposition: _Disposition) -> _Row:
    return _Row(name, re.compile(pattern), disposition)


#: The classifier. ORDERED, first match wins, and exhaustive over the fleet's corpus (10
#: plates, 9×H2S + 1×H2C, 24 distinct line shapes).
#:
#: The order is normative where the two prime rows precede ``lip_x_move``: a prime traverse
#: (``G1 X290 E20 F623.623``) must never be read as the lip-visit's bare X move, because
#: dropping it would delete the prime the whole rewrite exists to preserve. The patterns are
#: additionally full-matched, so the two cannot collide even if a row is one day relaxed.
_TABLE: tuple[_Row, ...] = (
    # PRIME — ahead of every lip-visit row.
    _row("prime_traverse", rf"G1\s+X{_NUM}\s+E(?P<e>{_POS})\s+F{_NUM}", "prime_traverse"),
    _row("prime_stationary", rf"G1\s+E(?P<e>{_POS})\s+F{_NUM}", "prime_stationary"),
    # LIP-VISIT — the group that only exists to reach, touch and leave the plate lip.
    _row("lip_z_approach", rf"G1\s+Z{_NUM}\s+F{_NUM}", "drop"),
    _row("lip_xy_approach", rf"G1\s+X{_NUM}\s+Y{_NUM}\s+F{_NUM}", "drop"),
    _row("lip_x_move", rf"G1\s+X{_NUM}\s+F{_NUM}", "drop"),
    _row("lip_peel_arc", rf"G3\s+Z{_NUM}\s+I{_NUM}\s+J{_NUM}\s+P{_NUM}\s+F{_NUM}", "drop"),
    _row("lip_pre_extrude_z_recall", rf"G28\.14\s+R{_NUM}", "drop"),
    _row("lip_z_comp_off", r"G29\.2\s+S0", "drop"),
    _row("lip_relative_mode", r"G91", "drop"),
    # STATE — modal and thermal settings the relocated prime still needs.
    _row("state_z_comp_on", r"G29\.2\s+S1", "keep"),
    _row("state_absolute_mode", r"G90", "keep"),
    _row("state_extruder_relative", r"M83", "keep"),
    _row("state_set_temperature", rf"M104\s+S{_NUM}", "keep"),
    _row("state_wait_temperature", rf"M109\s+S{_NUM}", "keep"),
    # Bare / P / S forms of the sync. The P and S forms also belong to the H2C
    # record-and-persist group; the disposition is the same either way.
    _row("state_sync", rf"M400(?:\s+[PS]{_NUM})?", "keep"),
    _row("state_progress", rf"M73\s+P{_NUM}\s+R{_NUM}", "keep"),
    # RECORD-AND-PERSIST — H2C's calibration bookkeeping, kept in place and in order.
    _row("record_banner", r"=+\s*record data\s*=+", "keep"),
    _row("record_mark", r"M1026", "keep"),
    _row("record_store_low", r"M1012\.8", "keep"),
    _row("record_store_high", r"M1012\.9", "keep"),
    _row("record_z_cali", r"G29\.9", "keep"),
    _row("record_persist", rf"M500\s+D{_NUM}", "keep"),
)

#: The shape the vendor's wiper exit takes, in file order, immediately above the open
#: marker. Replayed verbatim after the relocated prime so the toolhead leaves the chute the
#: way the slicer's own block leaves it.
_WIPER_EXIT_SHAPE: tuple[str, ...] = (
    r"G90",
    rf"G1\s+Z{_NUM}\s+F{_NUM}",
    rf"G1\s+Y{_NUM}\s+F{_NUM}",
    rf"G1\s+Y{_NUM}\s+F{_NUM}",
)

_WIPER_EXIT_PATTERNS = tuple(re.compile(p) for p in _WIPER_EXIT_SHAPE)

#: Removes the ``X`` word — and the whitespace that introduces it — from a prime traverse,
#: leaving indentation, the ``E``/``F`` words and any trailing comment byte-identical.
_X_WORD_RE = re.compile(rf"\s+X{_NUM}")


def _code_part(line: str) -> str:
    """The executable part of a section line: indentation removed, trailing comment cut.

    A comment-ONLY line is returned verbatim rather than as an empty string, so the table
    refuses it (``unknown_line``) instead of the blank-line arm silently swallowing it.
    """
    body = line.lstrip()
    if body.startswith(";"):
        return body
    return body.partition(";")[0].rstrip()


def _classify(code: str) -> tuple[_Row, re.Match[str]] | None:
    """First matching table row and its match, or None when the shape is unknown."""
    for row in _TABLE:
        m = row.pattern.fullmatch(code)
        if m is not None:
            return row, m
    return None


def _is_open_marker(line: str) -> bool:
    body = line.lstrip()
    return body.startswith(_SECTION_OPEN_PREFIX) and not _is_close_marker(line)


def _is_close_marker(line: str) -> bool:
    body = line.lstrip()
    if not body.startswith(_SECTION_CLOSE_PREFIX):
        return False
    return any(spelling in body for spelling in _SECTION_CLOSE_SPELLINGS)


def _index_of_marker(lines: list[str], marker: str) -> int | None:
    for i, line in enumerate(lines):
        if line.startswith(marker):
            return i
    return None


def _section_indent(body: list[str]) -> str:
    """The indentation new lines are emitted at: that of the section's first real line."""
    for line in body:
        if line.strip():
            return line[: len(line) - len(line.lstrip())]
    return ""


def _uses_macro(lines: list[str], macro: str) -> bool:
    """True when some line's executable part STARTS with ``macro`` as its own token."""
    for line in lines:
        words = _code_part(line).split()
        if words and words[0] == macro:
            return True
    return False


def _wiper_exit(lines: list[str], open_idx: int, window_start: int) -> list[str] | None:
    """The vendor's four-line wiper exit immediately above the open marker, in file order.

    Blank lines are skipped; anything else counts as one of the four and must match its
    slot in :data:`_WIPER_EXIT_SHAPE`. Returns the executable parts (to be re-indented at
    the section's own column), or None when the block is not the shape we know.
    """
    collected: list[str] = []
    i = open_idx - 1
    while i >= window_start and len(collected) < len(_WIPER_EXIT_PATTERNS):
        if lines[i].strip():
            collected.append(_code_part(lines[i]))
        i -= 1
    if len(collected) < len(_WIPER_EXIT_PATTERNS):
        return None
    collected.reverse()
    for code, pattern in zip(collected, _WIPER_EXIT_PATTERNS, strict=True):
        if not pattern.fullmatch(code):
            return None
    return collected


def rewrite_head(head: bytes) -> ChutePrimeOutcome:
    """Relocate the start block's prime bead from the plate lip into the purge chute.

    ``head`` is the plate G-code from byte 0 through the ``; MACHINE_START_GCODE_END``
    line — every outcome is decidable from it, which is why the reader that produces it
    (``threemf_tools.read_plate_gcode_start_block``) is bounded. Pure: no I/O, no clock, no
    logging; the same bytes always give the same outcome.

    Invariants a caller may rely on when the outcome is :class:`Rewritten`: everything
    outside the nozzle-load-line section is byte-identical (so ``full.startswith(head)``
    still splices), line endings and the trailing newline are preserved, ΣE over the
    section is unchanged, no ``X``/``Y``/``Z`` word survives inside the section except in
    the replayed wiper exit, ``G91`` is never left in force, and the section still ends
    ``G90`` / ``M83`` / ``G29.2 S1``.
    """
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Refused("undecodable", f"head is not valid UTF-8: {exc}")

    # Split without consuming the separator so a rejoin is byte-exact: a CRLF head keeps
    # its "\r" on every element, and a trailing newline keeps its empty final element.
    lines = text.split("\n")

    window_start = _index_of_marker(lines, _EXEC_BLOCK_START_MARKER)
    window_end = _index_of_marker(lines, _START_GCODE_END_MARKER)
    if window_start is None or window_end is None or window_end <= window_start:
        return Refused(
            "no_start_block",
            f"no {_EXEC_BLOCK_START_MARKER} … {_START_GCODE_END_MARKER} window in the head",
        )

    window = range(window_start, window_end + 1)
    opens = [i for i in window if _is_open_marker(lines[i])]
    closes = [i for i in window if _is_close_marker(lines[i])]
    if not opens and not closes:
        return Refused("section_missing", "the start block carries no nozzle-load-line section")
    if len(opens) != 1 or len(closes) != 1 or opens[0] >= closes[0]:
        return Refused(
            "section_ambiguous",
            f"expected one ordered marker pair, found {len(opens)} open / {len(closes)} close",
        )

    open_idx, close_idx = opens[0], closes[0]
    section = lines[open_idx : close_idx + 1]
    if any(_MARKER_TOKEN in line for line in section):
        return AlreadyRewritten()

    body = lines[open_idx + 1 : close_idx]
    indent = _section_indent(body)
    # A CRLF head's lines carry a trailing "\r"; emitted lines must carry it too or the
    # rewritten section would land with mixed endings.
    eol = "\r" if lines[open_idx].endswith("\r") else ""

    # Classify every body line first: a single unknown shape refuses the whole file, so
    # nothing is emitted until the entire section is understood. None marks a blank line.
    classified: list[tuple[_Row, re.Match[str]] | None] = []
    for line in body:
        if not line.strip():
            classified.append(None)
            continue
        hit = _classify(_code_part(line))
        if hit is None:
            return Refused("unknown_line", line)
        classified.append(hit)

    # One pass over the classified body collects every index the rules below ask about.
    # Collected here rather than in four comprehensions so the blank-line ``None`` is
    # unwrapped ONCE — a comprehension that re-indexes ``classified`` has to re-assert
    # that non-None-ness to a type checker, and an ignore comment is how that goes wrong.
    prime_idx: list[int] = []
    traverse_idx: list[int] = []
    drop_idx: list[int] = []
    wait_idx: list[int] = []
    for i, hit in enumerate(classified):
        if hit is None:
            continue
        row = hit[0]
        if row.disposition.startswith("prime"):
            prime_idx.append(i)
            if row.disposition == "prime_traverse":
                traverse_idx.append(i)
        elif row.disposition == "drop":
            drop_idx.append(i)
        if row.name == "state_wait_temperature":
            wait_idx.append(i)

    if len(traverse_idx) != 1:
        return Refused(
            "no_prime_extrusion",
            f"expected exactly one prime traverse, found {len(traverse_idx)}",
        )

    first_prime, last_prime = min(prime_idx), max(prime_idx)
    if not any(i < first_prime for i in wait_idx):
        return Refused("no_temperature_wait", "nothing waits for print temperature before the prime")

    if not drop_idx:
        # Without a lip visit there is no position to put the chute park at, and priming
        # wherever the toolhead happens to stand is exactly the accident this module
        # prevents. The section is not the shape we know, so it is left alone.
        return Refused("no_lip_visit", "no lip-visit line to anchor the chute prime")
    if drop_idx[0] > first_prime:
        # The park is emitted where the FIRST dropped line stood. Behind the prime, that
        # would send the toolhead to the chute AFTER extruding — the bead would land
        # wherever the nozzle happened to be, which is worse than the plate lip this
        # module is clearing, because at least the lip is a known place.
        return Refused(
            "no_lip_visit",
            f"the first lip-visit line (body line {drop_idx[0]}) follows the first prime line "
            f"(body line {first_prime}), so the chute park would be emitted after the prime",
        )

    before_section = lines[window_start:open_idx]
    if not (_uses_macro(before_section, _CHUTE_PARK) and _uses_macro(before_section, _CHUTE_WIPE)):
        return Refused(
            "macros_unwitnessed",
            f"the start block never uses both {_CHUTE_PARK} and {_CHUTE_WIPE}",
        )

    exit_lines = _wiper_exit(lines, open_idx, window_start)
    if exit_lines is None:
        return Refused(
            "wiper_exit_unrecognised",
            "the four code lines above the open marker are not the vendor wiper exit",
        )

    emitted: list[str] = [lines[open_idx]]
    prime_mm = 0.0
    for i, line in enumerate(body):
        hit = classified[i]
        if hit is None:
            emitted.append(line)
        else:
            row, match = hit
            if row.disposition == "drop":
                if i == drop_idx[0]:
                    emitted.append(f"{indent}{_MARKER_COMMENT}{eol}")
                    emitted.append(f"{indent}{_CHUTE_PARK}{eol}")
            elif row.disposition == "prime_traverse":
                prime_mm += float(match.group("e"))
                emitted.append(_X_WORD_RE.sub("", line, count=1))
            else:
                if row.disposition == "prime_stationary":
                    prime_mm += float(match.group("e"))
                emitted.append(line)
        if i == last_prime:
            emitted.append(f"{indent}{_SYNC}{eol}")
            emitted.append(f"{indent}{_CHUTE_WIPE}{eol}")
            emitted.extend(f"{indent}{code}{eol}" for code in exit_lines)
    emitted.append(lines[close_idx])

    rewritten = lines[:open_idx] + emitted + lines[close_idx + 1 :]
    return Rewritten("\n".join(rewritten).encode("utf-8"), prime_mm)
