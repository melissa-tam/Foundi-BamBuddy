"""The ONE slot-identity builder — ``services.slot_identity``.

Four lanes write an AMS slot's filament (internal assign, the farm's Configure Slot
endpoint, and both Spoolman lanes) and three of them used to compose the wire fields
themselves. The firmware pairs backup slots on an EXACT preset + colour match, so two
lanes composing the same filament a hair differently is a split auto-refill group and a
printer that runs a slot dry beside a full one (011-H2S ``GFG99``, 010-H2S ``161616FF``).

These cases pin the ladder itself, so a lane can be re-pointed at it without anyone
having to re-derive what it does.
"""

from __future__ import annotations

import pytest

from backend.app.services.slot_identity import (
    DEFAULT_TRAY_COLOR,
    compose_sub_brands,
    normalize_tray_color,
    resolve_slot_identity,
)


@pytest.fixture
def settings(monkeypatch):
    """A mutable settings backing for the resolver's ONE settings read.

    ``tagless_default_filament`` defaults to the OPERATOR'S EXPLICIT OFF (empty string),
    not to unset: unset resolves to the schema default (Bambu Lab PETG HF / ``GFG02``),
    the feature being on by default, and that default's canonical-identity substitution
    would rewrite every id these ladder cases are trying to observe. The one case that
    wants it on turns it on.
    """
    values: dict[str, str] = {"tagless_default_filament": ""}

    async def fake_get_setting(_db, key):
        return values.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    return values


async def _resolve(db, **kwargs):
    base = {
        "db": db,
        "current_user": None,
        "material": "PETG",
        "slicer_filament": None,
        "slicer_filament_name": None,
        "rgba": "000000FF",
        "nozzle_temp_min": 220,
        "nozzle_temp_max": 260,
        "sub_brands": "PETG HF",
    }
    base.update(kwargs)
    return await resolve_slot_identity(**base)


# --- the filament-reference ladder ------------------------------------------


@pytest.mark.asyncio
async def test_a_stated_preset_is_published_verbatim(db_session, settings):
    """Tier 1. An explicit preset is an operator STATEMENT about the roll, so it never
    goes through the resolver's sanitiser — which would discard a PFUS cloud id, the
    exact value the Configure Slot dialog exists to send."""
    identity = await _resolve(
        db_session,
        stated_tray_info_idx="PFUS9ac902733670a9",
        stated_setting_id="PFUS9ac902733670a9",
    )

    assert identity.tray_info_idx == "PFUS9ac902733670a9"
    assert identity.setting_id == "PFUS9ac902733670a9"


@pytest.mark.asyncio
async def test_a_stated_preset_outranks_the_live_tray_and_the_generic(db_session, settings):
    identity = await _resolve(
        db_session,
        stated_tray_info_idx="GFL05",
        current_tray_info_idx="GFG02",
        current_tray_type="PETG",
    )

    assert identity.tray_info_idx == "GFL05"


@pytest.mark.asyncio
async def test_the_rows_own_reference_resolves_when_nothing_was_stated(db_session, settings):
    """Tier 2 — the resolver owns GF normalise / cloud lookup / LocalPreset."""
    identity = await _resolve(db_session, slicer_filament="GFG02")

    assert identity.tray_info_idx == "GFG02"
    assert identity.setting_id == "GFSG02"


@pytest.mark.asyncio
async def test_a_specific_preset_already_on_the_slot_is_kept(db_session, settings):
    """Tier 3. Overwriting a slot's specific preset with a generic is what splits the
    backup group, so an unresolvable row defers to what the tray already carries."""
    identity = await _resolve(db_session, current_tray_info_idx="GFG02", current_tray_type="PETG")

    assert identity.tray_info_idx == "GFG02"
    assert identity.setting_id == "GFSG02"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_id", "live_type"),
    [
        ("PFUS9ac902733670a9", "PETG"),  # slicer rejects a PFUS as tray_info_idx
        ("PFCN1234567890abcd", "PETG"),  # same shape problem, partner presets
        ("PETG", "PETG"),  # a literal material name is never a preset
        ("GFG99", "PETG"),  # already generic — nothing to preserve
        ("GFG02", "PLA"),  # a DIFFERENT material's preset
    ],
)
async def test_an_unusable_live_id_is_not_reused(db_session, settings, live_id, live_type):
    identity = await _resolve(db_session, current_tray_info_idx=live_id, current_tray_type=live_type)

    assert identity.tray_info_idx == "GFG99", "fell through to the resolver's own generic tier"
    assert identity.setting_id == "GFSG99"


@pytest.mark.asyncio
async def test_nothing_resolvable_falls_to_the_resolvers_own_generic(db_session, settings):
    """Tier 4. Composing the generic id at a CALL SITE is what published the bare GFG99
    behind the 011-H2S no-auto-refill incident: it skips the canonical-identity
    substitution that would have landed the fleet's specific id instead. The fallback is
    a re-entry into the resolver, so every caller inherits that substitution."""
    identity = await _resolve(db_session, material="PLA", slicer_filament=None)

    assert identity.tray_info_idx == "GFL99"
    assert identity.setting_id == "GFSL99"


@pytest.mark.asyncio
async def test_a_material_with_no_generic_id_resolves_nothing(db_session, settings):
    identity = await _resolve(db_session, material="UNOBTAINIUM")

    assert identity.tray_info_idx == ""
    assert identity.setting_id == ""


@pytest.mark.asyncio
async def test_the_generic_tier_inherits_the_fleet_defaults_specific_identity(db_session, settings):
    """The reason tier 4 re-enters the resolver instead of returning a composed id.

    With the tagless default configured (the shipped state — the feature is ON unless an
    operator empties it), a row that resolves nothing but fingerprint-matches the fleet
    default comes back as that default's SPECIFIC identity: id, setting_id AND its nozzle
    range. A caller that looked the generic up itself would publish a bare ``GFG99`` and
    split the backup group against every slot already carrying ``GFG02``.
    """
    del settings["tagless_default_filament"]  # unset → the schema default, feature on

    identity = await _resolve(db_session, material="PETG", rgba="000000FF")

    assert identity.tray_info_idx == "GFG02", "the fleet's specific preset, not the generic"
    assert identity.setting_id == "GFSG02"
    assert (identity.nozzle_temp_min, identity.nozzle_temp_max) == (230, 270)


# --- the fields that are grouping DIMENSIONS ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("ff0000ff", "FF0000FF"),  # case is not cosmetic: the firmware matches bytes
        ("FF0000", "FF0000FF"),  # a 6-digit store is the same colour, 8 digits on the wire
        ("FF0000FF", "FF0000FF"),
        (None, DEFAULT_TRAY_COLOR),
        ("", DEFAULT_TRAY_COLOR),
        ("nonsense", DEFAULT_TRAY_COLOR),
    ],
)
async def test_colour_reaches_the_wire_normalised(db_session, settings, stored, expected):
    identity = await _resolve(db_session, rgba=stored)

    assert identity.tray_color == expected
    assert normalize_tray_color(stored) == expected


@pytest.mark.asyncio
async def test_the_rows_temps_ride_through_untouched(db_session, settings):
    identity = await _resolve(db_session, nozzle_temp_min=235, nozzle_temp_max=265)

    assert (identity.nozzle_temp_min, identity.nozzle_temp_max) == (235, 265)


@pytest.mark.asyncio
async def test_absent_temps_fall_back_to_the_material_table(db_session, settings):
    """Both halves independently — a row storing only a minimum keeps it."""
    identity = await _resolve(db_session, material="PLA", nozzle_temp_min=None, nozzle_temp_max=None)
    assert (identity.nozzle_temp_min, identity.nozzle_temp_max) == (190, 230)

    half = await _resolve(db_session, material="PLA", nozzle_temp_min=205, nozzle_temp_max=None)
    assert (half.nozzle_temp_min, half.nozzle_temp_max) == (205, 230)


@pytest.mark.asyncio
async def test_the_callers_sub_brand_label_survives_when_nothing_more_specific_resolves(db_session, settings):
    identity = await _resolve(db_session, sub_brands="Bambu Lab PETG HF")

    assert identity.tray_sub_brands == "Bambu Lab PETG HF"
    assert identity.tray_type == "PETG"


# --- the shared label composition -------------------------------------------


@pytest.mark.parametrize(
    ("brand", "material", "subtype", "expected"),
    [
        ("Bambu Lab", "PETG", "HF", "Bambu Lab PETG HF"),
        ("Bambu Lab", "PETG", None, "Bambu Lab PETG"),
        (None, "PETG", "HF", "PETG HF"),
        (None, "PETG", None, "PETG"),
        (None, "PETG", "   ", "PETG"),
        (None, None, None, ""),
    ],
)
def test_sub_brands_compose_identically_for_every_lane(brand, material, subtype, expected):
    assert compose_sub_brands(brand, material, subtype) == expected
