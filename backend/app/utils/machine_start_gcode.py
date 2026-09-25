"""Where a sliced plate's executable machine-start block is, and which part of a line executes.

Pure text grammar, no I/O. Two per-dispatch rewrites edit the Bambu Studio machine-start
block — the chute prime (:mod:`backend.app.services.chute_prime`) and the plate blow-off
(:mod:`backend.app.services.plate_blowoff`) — and both must agree on the same two facts
before either may touch a byte; the container layer (:mod:`backend.app.utils.threemf_tools`
— the bounded start-block reader and the upstream start-snippet anchor) reads the same end
marker. They live HERE, once, because the failure they guard against is silent: a rewrite
that searched the whole member would match the slicer's own QUOTED copy of the start block
and refuse every file, or worse, edit it.

**The executable window.** Every ``*.gcode.3mf`` plate member carries the start block
twice. The CONFIG_BLOCK line ``; machine_start_gcode = …`` quotes the UNRENDERED template on
one line (H2S :315, H2C :312 in the ``fixtures/chute_prime`` heads) — both branches of every
``{if}``, so it spells ``;===== bed leveling``, ``;===== nozzle load line`` and the heating
airduct branch's ``M145 P1`` on every file — and ``; machine_end_gcode = …`` (H2S :291,
H2C :288) quotes ``M145 P1`` again. Only the RENDERED copy executes, and it sits between
``; EXECUTABLE_BLOCK_START`` (H2S :578, H2C :575) and ``; MACHINE_START_GCODE_END``
(H2S :1016, H2C :1122). Both markers are matched at column 0 by prefix, so a CRLF line's
trailing ``\\r`` does not hide them and the config block's lower-case keys never match.

**A line's executable part.** Indentation is the slicer rendering ``{if}`` nesting, and a
``;`` starts a comment the firmware never sees — ``M145 P0 ; set airduct mode …`` executes
as ``M145 P0``, and ``;M73 P99`` executes as nothing. A rewrite that reads commands must
therefore read this part and only this part.
"""

from __future__ import annotations

from collections.abc import Sequence

EXEC_BLOCK_START_MARKER = "; EXECUTABLE_BLOCK_START"
START_GCODE_END_MARKER = "; MACHINE_START_GCODE_END"


def _index_of_marker(lines: Sequence[str], marker: str) -> int | None:
    for i, line in enumerate(lines):
        if line.startswith(marker):
            return i
    return None


def executable_window(lines: Sequence[str]) -> tuple[int, int] | None:
    """``(start, end)`` line indices of the rendered machine-start block, both inclusive.

    ``start`` is the ``; EXECUTABLE_BLOCK_START`` line and ``end`` the FIRST
    ``; MACHINE_START_GCODE_END`` line. None when either is missing or they are out of
    order — a file that is not a Bambu Studio print, or one truncated inside its header,
    and in either case not one a start-block rewrite may edit.
    """
    start = _index_of_marker(lines, EXEC_BLOCK_START_MARKER)
    end = _index_of_marker(lines, START_GCODE_END_MARKER)
    if start is None or end is None or end <= start:
        return None
    return start, end


def code_part(line: str) -> str:
    """The executable part of a start-block line: indentation removed, trailing comment cut.

    A comment-ONLY line is returned verbatim rather than as an empty string. A caller that
    classifies lines then sees an unknown shape and refuses (chute prime's
    ``unknown_line``) instead of mistaking the comment for a blank line; a caller that
    reads the first word sees one starting with ``;``, which no command matches.
    """
    body = line.lstrip()
    if body.startswith(";"):
        return body
    return body.partition(";")[0].rstrip()
