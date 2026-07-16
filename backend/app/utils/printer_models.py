"""Printer model normalization utilities.

Converts 3MF printer model names (e.g., "Bambu Lab X1 Carbon") to
normalized short names (e.g., "X1C") that match database storage.
"""

from typing import Any

# Map from 3MF printer_model strings to normalized short names
PRINTER_MODEL_MAP = {
    "Bambu Lab X1 Carbon": "X1C",
    "Bambu Lab X1": "X1",
    "Bambu Lab X1E": "X1E",
    "Bambu Lab P1S": "P1S",
    "Bambu Lab P1P": "P1P",
    "Bambu Lab P2S": "P2S",
    "Bambu Lab A1": "A1",
    "Bambu Lab A1 Mini": "A1 Mini",
    "Bambu Lab A1 mini": "A1 Mini",
    # Bambu cloud rolled out a terse model-code rename mid-2026 (#1649);
    # 3MFs prepared with newer cloud presets may carry this short form.
    "Bambu Lab A1M": "A1 Mini",
    "Bambu Lab H2D": "H2D",
    "Bambu Lab H2D Pro": "H2D Pro",
    "Bambu Lab H2C": "H2C",
    "Bambu Lab H2S": "H2S",
    "Bambu Lab X2D": "X2D",
    "Bambu Lab A2L": "A2L",
}

# Map from printer_model_id (internal codes in slice_info.config) to short names
# These are the codes Bambu Studio uses internally
PRINTER_MODEL_ID_MAP = {
    # X1 series
    "C11": "X1C",
    "C12": "X1",
    "C13": "X1E",
    # P1 series
    "P1P": "P1P",
    "P1S": "P1S",
    # P2 series
    "P2S": "P2S",
    "N7": "P2S",  # N7 is the SSDP/device code for the P2S (no slice-semantics collision — it already appears as P2S in the steel-rod set)
    # X2 series
    "N6": "X2D",
    # A2 series (A2L is single-FDM + integrated cutter/plotter — single nozzle)
    "N9": "A2L",
    # A1 series
    "A11": "A1",
    "A12": "A1 Mini",
    "N1": "A1 Mini",
    "N2S": "A1",
    "A04": "A1 Mini",
    # H2 series (Office/H series)
    "O1D": "H2D",
    "O1E": "H2D Pro",  # Some devices report O1E
    "O2D": "H2D Pro",  # Some devices report O2D
    "O1C": "H2C",
    "O1C2": "H2C",
    "O1S": "H2S",
}


# Rod/rail type classification for maintenance tasks.
# Carbon rods: X1, P1 series (CoreXY with carbon fiber rods)
# Steel rods: P2S, X2D series (hardened steel linear shafts)
# Linear rails: A1, H2 series (linear rail motion system)
# Values must be uppercase with spaces stripped for normalized comparison.
CARBON_ROD_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "X1",
        "X1C",
        "X1E",
        "P1P",
        "P1S",
        # Internal codes
        "C11",  # X1C
        "C12",  # X1
        "C13",  # X1E
    ]
)

STEEL_ROD_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "P2S",
        "X2D",
        # Internal codes
        "N7",  # P2S
        "N6",  # X2D
    ]
)

LINEAR_RAIL_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "A1",
        "A1MINI",
        "A2L",
        "H2D",
        "H2DPRO",
        "H2C",
        "H2S",
        # Internal codes
        "N1",  # A1 Mini
        "N2S",  # A1
        "N9",  # A2L
        "A04",  # A1 Mini (alternate)
        "A11",  # A1
        "A12",  # A1 Mini
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
    ]
)


# Models without any external storage (MicroSD / SD card slot).
# The A1 and A1 Mini ship with internal storage only — there is no
# firmware-side "Store sent files on external storage" toggle and no
# slicer-side equivalent surfaces one. The connection diagnostic's
# external_storage check (printer_diagnostic.py) must skip on these
# models instead of reporting fail from a 0-valued home_flag bit.
NO_EXTERNAL_STORAGE_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "A1",
        "A1MINI",
        # Internal codes
        "N1",  # A1 Mini
        "N2S",  # A1
        "A04",  # A1 Mini (alternate)
        "A11",  # A1
        "A12",  # A1 Mini
    ]
)


# Models with an ethernet port.
# X1, P1P, A1, A1 Mini do NOT have ethernet.
ETHERNET_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "X1C",
        "X1E",
        "X2D",
        "P1S",
        "P2S",
        "H2D",
        "H2DPRO",
        "H2C",
        "H2S",
        # Internal codes
        "C11",  # X1C
        "C13",  # X1E
        "N6",  # X2D
        "P1S",  # P1S
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "O1S",  # H2S
    ]
)


# Dual-nozzle (dual-extruder) printers. Single source of truth for nozzle
# class — consumed by ``BambuMQTTClient.start_print``, the K-profile routes,
# and the re-slice nozzle-class guard (previously an inline model tuple
# duplicated across all three). Re-slicing a model laid out for a single-nozzle
# printer onto one of these — or vice versa — is not yet supported: the source
# 3MF's embedded single-nozzle filament/extruder layout is not a valid
# dual-nozzle project and BambuStudio's multi-extruder validator rejects it.
DUAL_NOZZLE_MODELS = frozenset(
    [
        # Display names (uppercase, no spaces)
        "H2D",
        "H2DPRO",
        "H2C",
        "X2D",
        # Internal codes
        "O1D",  # H2D
        "O1E",  # H2D Pro
        "O2D",  # H2D Pro (alternate)
        "O1C",  # H2C
        "O1C2",  # H2C (dual nozzle variant)
        "N6",  # X2D
    ]
)


# Bed-slinger (open-frame) models — the bed moves in Y and the GANTRY carries the
# Z axis, so the toolhead (not the bed) moves in Z and the bed is FIXED in Z.
# ``A1_FAMILY_MODELS`` is the canonical A1-family set (previously duplicated as the
# private ``A1_MODELS`` literal in ``services.printer_manager``); it is the single
# source of truth both that module (its stg_cur idle-bug set) and this one consume.
# Spellings are kept verbatim (display + dash + space forms) so callers that
# normalise with ``.strip().upper()`` only (``has_stg_cur_idle_bug``) still match.
A1_FAMILY_MODELS = frozenset(
    [
        # Display names
        "A1",
        "A1 MINI",
        "A1-MINI",
        "A1MINI",
        # Internal codes (from MQTT/SSDP)
        "N1",  # A1 Mini
        "N2S",  # A1
    ]
)

# Every bed-slinger: the A1 family plus the A2L cutter/plotter hybrid (also bed-on-Y,
# gantry-on-Z). Membership is tested through the ``is_dual_nozzle_model``-shape
# normalisation (upper-cased, space/dash stripped), so the A2L display name is
# stored space-stripped ("BAMBULABA2L") to match; "N9" is the A2L SSDP/device code.
BEDSLINGER_MODELS = A1_FAMILY_MODELS | frozenset(
    {
        "A2L",
        "BAMBULABA2L",
        "N9",
    }
)


# Dual-nozzle (Vortek) homing forms — the single canonical source of truth for
# every farm surface that homes a dual-nozzle machine: the eject generator +
# validator, the dry-run wrapper, and manual/auto home (``BambuMQTTClient.home_axes``).
#
# INCIDENT (007-H2C, 2026-07-12): a bare `G28` / `G28 X Y` in the post-print
# no-tool state stall-loops on dual-nozzle H2-series firmware — the sensorless
# X-homing stall threshold is unsuited to the dual carriage, so the carriage rams
# the X-homing wall nonstop until emergency-stopped. The stock O1C2 start block
# NEVER homes unparameterized: it uses these torque-parameterized forms, where
# `T` is the stall-torque threshold. Copied VERBATIM from that stock start block;
# homes X then Y in two SEPARATE parameterized commands. Validated calm by the
# supervised motion-smoke ladder (2026-07-12).
DUAL_NOZZLE_HOME: tuple[str, str] = ("G28 X T300", "G28 Y T300")

# Full home for dual-nozzle models: the two X/Y torque forms plus the stock
# Z-home form. A Z-home probes the bed centre — safe ONLY when the bed is known
# empty (the dry-run wrapper's by-definition-empty bed, or a deliberate operator
# manual home), NEVER inside a production eject block that runs with a part on the
# plate. The `G28 Z P0 T250` form is ATTESTED in the O1C2 stock start block but was
# NOT individually micro-probed; it got its first live exercise at the supervised
# dry run.
DUAL_NOZZLE_FULL_HOME: tuple[str, ...] = (*DUAL_NOZZLE_HOME, "G28 Z P0 T250")


# MQTT extruder / Vortek-rack id space on dual-nozzle H2-series machines.
#
# PINNED by live hardware capture on 007-H2C (O1C2 firmware, 2026-07-16): with a
# 0.6 mm nozzle mounted on the RIGHT hotend and a 0.4 mm on the LEFT, the printer
# reported ``state.nozzles[0].nozzle_diameter == "0.6"`` — i.e. MQTT extruder id 0
# is the RIGHT/main hotend and id 1 is the LEFT/deputy. (The ``nozzles`` list
# comment at ``bambu_mqtt.py:315`` saying "0 = left" predates this capture and is
# wrong.) Entries in ``device.nozzle.info`` with an id ``>= 16`` are Vortek rack
# SLOTS, not mounted hotends (rack physical slot 1 ↔ id 16).
EXTRUDER_ID_RIGHT = 0
EXTRUDER_ID_LEFT = 1
RACK_ID_MIN = 16


def side_label(extruder_id: int | None) -> str:
    """Human side label for an MQTT extruder id (reason/diagnostic text only)."""
    if extruder_id == EXTRUDER_ID_RIGHT:
        return "right"
    if extruder_id == EXTRUDER_ID_LEFT:
        return "left"
    return "?"


def extruder_for_ams(ams_extruder_map: dict | None, ams_id: int, tray_id: int | None = None) -> int | None:
    """Resolve which MQTT extruder (0 = right/main, 1 = left/deputy) an AMS feeds.

    Single source of truth for the inline idiom previously duplicated at
    ``main.py:2062-2067`` and ``api/routes/inventory.py:188-192``: the
    external-spool virtual AMS (id 255) routes tray 0 → extruder 1 (left) and
    tray 1 → extruder 0 (right), i.e. ``1 - tray_id``; every other AMS id is
    looked up in the per-AMS ``ams_extruder_map`` (string-keyed). Returns
    ``None`` when the fact is unknown (external spool with no tray, or an AMS id
    absent from an empty/missing map).
    """
    if ams_id == 255:
        if tray_id is None:
            return None
        return 1 - tray_id
    if not ams_extruder_map:
        return None
    return ams_extruder_map.get(str(ams_id))


def nozzle_for_ams_unit(state: Any, ams_id: int, tray_id: int | None = None, default: str = "0.4") -> str:
    """Nozzle diameter (as a string, e.g. ``"0.4"``) of the extruder an AMS feeds.

    Resolves the serving extruder via :func:`extruder_for_ams` (falling back to
    extruder 0 when unresolved), then reads that mounted hotend's reported
    diameter from ``state.nozzles``. getattr-safe end to end: a missing state, a
    short/absent ``nozzles`` list, or an empty reported diameter all fall back to
    ``default``. Consolidates the ``state.nozzles[0].nozzle_diameter or "0.4"``
    idiom duplicated across the K-profile call sites.
    """
    eid = extruder_for_ams(getattr(state, "ams_extruder_map", None), ams_id, tray_id)
    if not isinstance(eid, int):
        eid = EXTRUDER_ID_RIGHT
    nozzles = getattr(state, "nozzles", None) or []
    if eid < 0 or eid >= len(nozzles):
        return default
    diameter = getattr(nozzles[eid], "nozzle_diameter", "") or ""
    diameter = diameter.strip() if isinstance(diameter, str) else str(diameter).strip()
    return diameter or default


def has_ethernet(model: str | None) -> bool:
    """Return True if the printer model has an ethernet port."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in ETHERNET_MODELS


def has_external_storage(model: str | None) -> bool:
    """Return True if the printer model can have a MicroSD / external storage slot.

    Defaults to True when the model is unknown — the diagnostic only flips
    its check on for the explicit no-storage list. New models added to the
    Bambu lineup without a slot must be added to ``NO_EXTERNAL_STORAGE_MODELS``
    or the diagnostic will continue to evaluate ``store_to_sdcard`` against
    a hardware feature the printer doesn't have.
    """
    if not model:
        return True
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized not in NO_EXTERNAL_STORAGE_MODELS


def is_dual_nozzle_model(model: str | None) -> bool:
    """Return True if the printer model has two nozzles (H2D family / X2D)."""
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in DUAL_NOZZLE_MODELS


def is_bedslinger_model(model: str | None) -> bool:
    """Return True if the printer is a bed-slinger — the bed moves in Y and the
    GANTRY carries the Z axis, so the toolhead (not the bed) moves in Z and the bed
    is FIXED in Z.

    Physics note (#1334): on these machines Z moves the TOOLHEAD, the bed is fixed
    in Z, and the G-code Z-direction semantics are INVERTED versus bed-on-Z
    machines — ``G1 Z-10`` drives the nozzle DOWN toward the fixed bed instead of
    raising the bed (the crash reported in #1334). Two consequences: the manual
    bed-jog route inverts the sign for these models, and the eject bed-drop release
    assist is physically meaningless (there is no bed-on-Z travel to open a
    part↔nozzle gap), so it must fail closed on them.

    Normalisation mirrors :func:`is_dual_nozzle_model` (upper-cased, space/dash
    stripped) so any spelling resolves against :data:`BEDSLINGER_MODELS`.
    """
    if not model:
        return False
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    return normalized in BEDSLINGER_MODELS


def full_home_lines(model: str | None) -> tuple[str, ...]:
    """Return the safe FULL-home (including Z) G-code line(s) for a model.

    Dual-nozzle models home with the parameterized torque forms plus the stock
    Z-home (:data:`DUAL_NOZZLE_FULL_HOME`) — an unparameterized ``G28`` stall-loops
    on that firmware (007-H2C ram incident, 2026-07-12). Every other model uses the
    single safe firmware ``G28``, which expands into the stock park→home-XY→home-Z
    sequence. Callers that must never home Z with a part on the plate (the eject
    prologue) use :data:`DUAL_NOZZLE_HOME` directly instead.
    """
    if is_dual_nozzle_model(model):
        return DUAL_NOZZLE_FULL_HOME
    return ("G28",)


def get_rod_type(model: str | None) -> str | None:
    """Return the rod/rail type for a printer model.

    Returns:
        "carbon" for X1/P1 series (carbon fiber rods),
        "steel_rod" for P2S/X2D series (hardened steel rods),
        "linear_rail" for A1/H2 series (linear rails),
        None for unknown models.
    """
    if not model:
        return None
    normalized = model.strip().upper().replace(" ", "").replace("-", "")
    if normalized in CARBON_ROD_MODELS:
        return "carbon"
    if normalized in STEEL_ROD_MODELS:
        return "steel_rod"
    if normalized in LINEAR_RAIL_MODELS:
        return "linear_rail"
    return None


def normalize_printer_model_id(model_id: str | None) -> str | None:
    """Convert printer_model_id (internal code) to normalized short name.

    Args:
        model_id: The printer_model_id from slice_info.config (e.g., "C11", "O1D")

    Returns:
        Normalized short name (e.g., "X1C", "H2D") or the original ID if unknown.
    """
    if not model_id:
        return None

    # Check known mappings
    if model_id in PRINTER_MODEL_ID_MAP:
        return PRINTER_MODEL_ID_MAP[model_id]

    # Return original if unknown (might already be a short name)
    return model_id


def normalize_printer_model(raw_model: str | None) -> str | None:
    """Convert 3MF printer_model to normalized short name.

    Args:
        raw_model: The printer_model string from 3MF metadata
            (e.g., "Bambu Lab X1 Carbon")

    Returns:
        Normalized short name (e.g., "X1C") or None if input is empty.
        Unknown models have "Bambu Lab " prefix stripped.
    """
    if not raw_model:
        return None

    # Check known mappings first
    if raw_model in PRINTER_MODEL_MAP:
        return PRINTER_MODEL_MAP[raw_model]

    # Strip "Bambu Lab " prefix for unknown models
    stripped = raw_model.replace("Bambu Lab ", "").strip()
    return stripped or None


def canon_model(value: str | None) -> str | None:
    """Collapse any model spelling (code / display name / short name) to one key.

    Runs the value through the internal-code map then the display-name map so a
    slice ``printer_model_id`` (e.g. ``O1S``), a g-code-header display name (e.g.
    ``Bambu Lab H2S``) and a stored ``Printer.model`` (e.g. ``H2S``) all land on
    the same uppercase, space-stripped token used for comparison and as the
    ``PrinterModelGeometry.model_key``. Returns ``None`` for empty/blank input.

    Single source of truth for model canonicalisation — the capability gate, the
    model-geometry accessor and the device-vs-declared mismatch check all use it,
    so ``O1S`` and ``H2S`` are never treated as different printers.
    """
    if not value or not str(value).strip():
        return None
    mapped = normalize_printer_model_id(str(value).strip())
    mapped = normalize_printer_model(mapped) or mapped
    return mapped.upper().replace(" ", "") if mapped else None
