"""Blow stray filament off the build plate with the auxiliary fan at print start.

Operator report: prints sometimes start on a plate carrying stray filament. The H2
auxiliary fan (``M106 P2``) is a fixed duct on the left wall whose stream is centred on
the nozzle plane, so once the plate is homed to that plane the stream runs straight across
it. This module inserts one full-speed pulse of that fan, once per print, at the point the
operator ruled (2026-09-25): just before bed leveling — after the start block's own
calibration purge and nozzle wipes at the chute, with the toolhead off the plate, and
with the fan stopped again before the leveling probe runs.

This module is the pure half: :func:`insert_blowoff` takes the plate G-code member and returns
it with the pulse inserted, or a closed refusal. No DB handle, no I/O, no logging — the
dispatch seam (:mod:`backend.app.services.dispatch_file`) owns the settings read, the
cache and the WARNING a refusal deserves.

**Where, with the file:line evidence** (absolute lines of ``Metadata/plate_1.gcode`` in
the ``fixtures/chute_prime`` heads: ``auto print test cube.gcode.3mf`` [H2S] and
``print cube h2c.gcode.3mf`` [H2C]). The pulse goes IMMEDIATELY BEFORE the
``;===== bed leveling`` banner — H2S :855, H2C :940. At that point:

* **The plate is at the nozzle plane.** ``G28 Z P0 T250`` (H2S :701, H2C :718) homes Z
  by bringing the plate up to the nozzle. On H2S no line between it and the banner
  commands a Z move — the one ``Z`` word there, ``M211 X0 Y0 Z0`` :740, switches the soft
  end stops off (the ``G150.x`` vendor macros in between are opaque). The foreign-object
  camera check ran EARLIER, with the plate low (``M972 S24`` :688 on both, before the Z
  home), so the pulse neither races it nor hides anything from it.
* **The start block's purge and wipes are done.** The flow-dynamics calibration
  (``;===== auto extrude cali`` H2S :772-814, H2C :846-888) and the flush ``M109`` →
  ``G1 E…`` → ``G1 E-3`` → ``G150.2`` → ``G150.1`` (H2S :819-827, H2C :894-903) run at
  the chute, then the temperature-parameterised wipe ``G150 T<temp>`` (H2S :841,
  H2C :917) leaves the toolhead at the wiper, off the plate.
* **Leveling has not started.** The banner's own section is where the vendor probes the
  plate (``G29 A1``/``G29 A2`` under ``M622 J1``/``J2``, H2S :871/:879, H2C :958/:966),
  and fan vibration disturbs a probe. So the pulse ENDS with ``M106 P2 S0`` before the
  banner — the section then switches the part fan off itself (``M106 S0 ; turn off fan ,
  too noisy``, H2S :861, H2C :946) and lowers the plate before probing.

**The idiom is the vendor's.** The start block's own cooling-airduct branch pulses the
same fan: ``M145 P0`` → ``M106 P2 S178`` → ``M191 S0`` → ``M106 P2 S0`` (H2S :640-647,
H2C :654-660). This module emits ``M400`` (let queued motion finish) → ``M106 P2 S255``
→ ``M400 S<seconds>`` → ``M106 P2 S0``. The dwell is ``M400 S``, never ``G4`` — the
dialect's dwell (``M400 S2`` :818 in the H2S flush), and the one the printer's time
estimate counts.

**Refusals — the file dispatches without the pulse** (closed :data:`PlateBlowoffRefusal`):

* the banner is absent from the window, or appears more than once;
* no ``G28`` with a ``Z`` word precedes it — nothing proves the plate is at the nozzle
  plane rather than wherever the previous job parked it;
* a bare ``G29`` precedes it — leveling already ran, so the "before leveling" premise is
  false. The word must be exactly ``G29``: ``G29.1 Z0`` (H2S :621), ``G29.2 S0`` (H2S
  :743) and ``G29.2 S1`` (H2S :766) precede the banner in every fixture head and are
  z-trim and z-compensation switches, not a probe;
* the airduct mode in force at the banner is not the vendor's cooling mode. The template
  renders ``{if (overall_chamber_temperature >= 40)}`` as ``M145 P1 ; set airduct mode to
  heating mode for heating`` + ``M106 P2 S0`` — the vendor deliberately keeps this fan OFF
  in heating mode, so the rewrite refuses ``heating_airduct`` there rather than blast a
  chamber the material wants warm. The mode in force is the LAST ``M145`` before the
  banner (``M145.2 P0 F1``, H2S :656, is a different command and is not read). NO ``M145``
  at all refuses too (``airduct_unwitnessed``): the finish tail's filtration branch
  leaves the duct in heating mode, so a file that never selects the mode may start in
  it. This is the same self-evidence rule as chute prime's ``macros_unwitnessed`` — the
  fan is pulsed only in a file whose own start block pulses it under the same duct mode.

**The H2C difference, not refused.** Just above the H2C banner a runtime-flagged
toolhead-offset calibration runs: ``M1002 judge_flag auto_cali_toolhead_offset_flag`` /
``M622 J0`` / ``G91`` / ``G1 Z5 F1200`` / ``G90`` / ``M1012.7`` / ``G383.7 U140 J0`` /
``M623`` (:926-933), and its ``J1``/``J2`` siblings (``G383`` / ``G383.3``, :808-824)
run earlier. Depending on the printer's flag the plate may sit 5 mm below the nozzle plane
and the toolhead wherever ``G383.7`` leaves it when the pulse runs. Whether the stream
still sweeps the plate there is unproven; the first H2C print with the pulse is the
witness (farm-ops Pending). The ``G1 Z5`` is relative and runtime-conditional, which is
why it is documented here rather than encoded as a refusal: the grammar cannot tell a
branch that runs from one that does not.

**Idempotent through the marker.** A member already carrying ``FARM PLATE BLOWOFF`` in its
window is :class:`AlreadyRewritten` — reachable in production, because ``foreign_archive``
archives the printer-resident, already rewritten copy of a screen-restarted job, which can
come back round as a donor. Such a donor keeps the pulse length it was built with.

The window is :func:`backend.app.utils.machine_start_gcode.executable_window` — the
CONFIG_BLOCK quotes both the banner and ``M145 P1`` on every file (H2S :315, H2C :312),
so a whole-member search would refuse everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypeAlias

from backend.app.utils.machine_start_gcode import (
    EXEC_BLOCK_START_MARKER,
    START_GCODE_END_MARKER,
    code_part,
    executable_window,
)

#: Identifies the recipe these bytes were produced by, and is stamped into the marker
#: comment. The dispatch seam folds it (with the pulse length) into the derived-artifact
#: cache key, so bumping it re-derives every cached file. Bump it whenever the emitted
#: block changes shape.
RECIPE_VERSION = "plate-blowoff-v1"

#: The substring that makes a rewritten member self-identifying, matched anywhere inside
#: the window so the comment's wording may evolve without breaking idempotence.
_MARKER_TOKEN = "FARM PLATE BLOWOFF"

_MARKER_COMMENT = f"; {_MARKER_TOKEN} ({RECIPE_VERSION})"

#: The banner of the vendor's leveling section, full-matched against the stripped line so
#: the section's closing banner (``;===== bed leveling end ====…``) is never the anchor.
_ANCHOR_RE = re.compile(r";=+\s*bed leveling\s*=+")

#: The auxiliary fan's ``M106`` index on H2S and H2C, and full speed.
_AUX_FAN = "P2"
_FULL_SPEED = "S255"

#: The airduct-mode command and its COOLING mode — the only mode the vendor itself runs
#: the auxiliary fan in during the start block.
_AIRDUCT_MODE = "M145"
_AIRDUCT_COOLING = "P0"
_AIRDUCT_HEATING = "P1"

#: Why a member was left alone. Closed on purpose, like chute prime's: a new way for this
#: to go wrong is added HERE, where the seam's WARNING and the test-per-reason can see it.
PlateBlowoffRefusal = Literal[
    "undecodable",  # the member is not strict UTF-8
    "no_start_block",  # no ; EXECUTABLE_BLOCK_START … ; MACHINE_START_GCODE_END window
    "anchor_missing",  # the window carries no ;===== bed leveling banner
    "anchor_ambiguous",  # the window carries more than one
    "no_z_home",  # no G28 … Z line precedes the banner
    "leveling_before_anchor",  # a bare G29 precedes the banner
    "heating_airduct",  # the airduct mode in force at the banner is heating (M145 P1)
    "airduct_unwitnessed",  # no M145 P0|P1 selects the airduct mode before the banner
]


@dataclass(frozen=True)
class Rewritten:
    """The member with the pulse inserted before the leveling banner.

    ``text`` is a drop-in replacement for the member handed in: every byte outside the
    five inserted lines is identical, line endings included. ``anchor_line`` is the
    1-based line number of the banner in the member handed in — where the pulse went,
    for the seam's log line.
    """

    text: str
    anchor_line: int


@dataclass(frozen=True)
class AlreadyRewritten:
    """The window carries our marker already — nothing to do, and not a problem."""


@dataclass(frozen=True)
class Refused:
    """The member was left alone. ``detail`` is for the log line, not for branching on."""

    reason: PlateBlowoffRefusal
    detail: str


PlateBlowoffOutcome: TypeAlias = Rewritten | AlreadyRewritten | Refused


def _words(line: str) -> list[str]:
    return code_part(line).split()


def _is_anchor(line: str) -> bool:
    return _ANCHOR_RE.fullmatch(line.strip()) is not None


def _is_z_home(words: list[str]) -> bool:
    return bool(words) and words[0] == "G28" and any(w.startswith("Z") for w in words[1:])


def _airduct_refusal(before_anchor: list[list[str]]) -> Refused | None:
    """None when the duct mode in force at the banner is cooling; the refusal otherwise."""
    selections = [words for words in before_anchor if words and words[0] == _AIRDUCT_MODE]
    if not selections:
        return Refused("airduct_unwitnessed", f"no {_AIRDUCT_MODE} selects the airduct mode before the banner")
    in_force = selections[-1][1:]
    if in_force == [_AIRDUCT_COOLING]:
        return None
    if in_force == [_AIRDUCT_HEATING]:
        return Refused(
            "heating_airduct",
            f"{_AIRDUCT_MODE} {_AIRDUCT_HEATING} (heating) is in force at the banner — the vendor keeps the "
            "auxiliary fan off in that mode",
        )
    return Refused(
        "airduct_unwitnessed",
        f"the airduct selection in force at the banner is {' '.join(selections[-1])!r}, not a mode this recipe knows",
    )


def insert_blowoff(member: str | bytes, *, seconds: int) -> PlateBlowoffOutcome:
    """Insert a ``seconds``-long auxiliary-fan pulse immediately before the leveling banner.

    ``member`` is the whole plate G-code — the dispatch seam hands over the member AS THE
    PREVIOUS STEP LEFT IT, so the chute prime's rewrite (which edits only the nozzle-load
    section, BELOW the banner) is already in it. Only the head through the
    ``; MACHINE_START_GCODE_END`` line is split and searched; the print body is carried
    through as one string. ``seconds`` is the dwell, a positive whole number the caller has
    already range-checked against the setting's schema.

    Pure: no I/O, no clock, no logging; the same input always gives the same outcome.
    """
    if isinstance(member, bytes):
        try:
            text = member.decode("utf-8")
        except UnicodeDecodeError as exc:
            return Refused("undecodable", f"member is not valid UTF-8: {exc}")
    else:
        text = member

    # Bound the work to the head. The first LINE-START end marker closes the start block;
    # the CONFIG_BLOCK's quoted copies sit mid-line and cannot match "\n" + marker.
    marker_at = text.find("\n" + START_GCODE_END_MARKER)
    if marker_at == -1:
        return Refused("no_start_block", f"no {START_GCODE_END_MARKER} line in the member")
    line_end = text.find("\n", marker_at + 1)
    head_end = len(text) if line_end == -1 else line_end + 1
    head, body = text[:head_end], text[head_end:]

    # Split without consuming the separator so the rejoin is byte-exact: a CRLF member
    # keeps its "\r" on every element, and the head's trailing newline its empty element.
    lines = head.split("\n")
    bounds = executable_window(lines)
    if bounds is None:
        return Refused("no_start_block", f"no {EXEC_BLOCK_START_MARKER} … {START_GCODE_END_MARKER} window")
    window_start, window_end = bounds
    window = range(window_start, window_end + 1)

    if any(_MARKER_TOKEN in lines[i] for i in window):
        return AlreadyRewritten()

    anchors = [i for i in window if _is_anchor(lines[i])]
    if not anchors:
        return Refused("anchor_missing", "the start block carries no ;===== bed leveling banner")
    if len(anchors) > 1:
        numbers = ", ".join(str(i + 1) for i in anchors)
        return Refused("anchor_ambiguous", f"{len(anchors)} bed-leveling banners in the start block (lines {numbers})")
    anchor = anchors[0]

    before_anchor = [_words(lines[i]) for i in range(window_start, anchor)]
    if not any(_is_z_home(words) for words in before_anchor):
        return Refused("no_z_home", "no G28 … Z homes the plate to the nozzle plane before the banner")
    leveled = next((i for i, words in enumerate(before_anchor) if words and words[0] == "G29"), None)
    if leveled is not None:
        return Refused(
            "leveling_before_anchor",
            f"G29 at line {window_start + leveled + 1} levels the bed before the banner",
        )
    duct = _airduct_refusal(before_anchor)
    if duct is not None:
        return duct

    banner = lines[anchor]
    indent = banner[: len(banner) - len(banner.lstrip())]
    # A CRLF member's lines carry a trailing "\r"; the emitted lines must carry it too.
    eol = "\r" if banner.endswith("\r") else ""
    pulse = [
        _MARKER_COMMENT,
        "M400",
        f"M106 {_AUX_FAN} {_FULL_SPEED}",
        f"M400 S{seconds}",
        f"M106 {_AUX_FAN} S0",
    ]
    lines[anchor:anchor] = [f"{indent}{code}{eol}" for code in pulse]
    return Rewritten("\n".join(lines) + body, anchor + 1)
