"""Generic-id substitution at the AMS write site — ``inventory.apply_spool_to_slot_via_mqtt``.

2026-07-25 PROD (011-H2S, printer id 9): three slots were published
``tray_info_idx=GFG99, setting_id=GFSG99`` every ~30 s for 13 minutes while a fourth
slot on the same printer got ``GFG02`` in the same pass. A generic id splits the
firmware's auto-refill backup group — it pairs slots only on an exact brand-class /
type / colour / nozzle-temps match — which is the 011-H2S "refused to auto-switch on
runout" cause the 2026-07-19 wave was supposed to have closed with ONE substitution
chokepoint (``spool_tagless.override_generic_identity``, applied inside
``resolve_slicer_filament``).

The escape: every offending row carries ``slicer_filament = ''`` (fleet-wide, NO row
stores the literal "GFG99"). ``resolve_slicer_filament`` returns early on an empty
value — 145 lines BEFORE the chokepoint — so the write site's own generic-material
fallback composed ``GENERIC_FILAMENT_IDS["PETG"] = "GFG99"`` outside it. The live tray
already reading ``GFG99`` cannot rescue it either: that branch rejects generic values,
so it falls through and re-publishes the same generic id forever. The wire tell is a
SPLIT identity — ``GFG99`` with the tagless default's 230/270 temps (temps resolve
before the early return), which is exactly what both affected printers showed.

The fix re-enters the ONE resolver with the composed id rather than re-implementing the
substitution at the write site.
"""

import json
from types import SimpleNamespace

import pytest

from backend.app.api.routes.inventory import apply_spool_to_slot_via_mqtt
from backend.app.models.spool import Spool

pytestmark = pytest.mark.asyncio

# The stored production tagless default (verified live 2026-07-25).
_TAGLESS_DEFAULT = {
    "brand": "Bambu Lab",
    "material": "PETG",
    "subtype": "HF",
    "rgba": "000000FF",
    "slicer_filament": "GFG02",
    "nozzle_temp_min": 230,
    "nozzle_temp_max": 270,
}


class _FakeClient:
    """Captures the ams_filament_setting the farm actually puts on the wire."""

    def __init__(self):
        self.settings: list[dict] = []
        self.cali: list[dict] = []

    def ams_set_filament_setting(self, **kw):
        self.settings.append(kw)
        return True

    def extrusion_cali_sel(self, **kw):
        self.cali.append(kw)
        return True


@pytest.fixture
def env(monkeypatch):
    settings = {"tagless_default_filament": json.dumps(_TAGLESS_DEFAULT)}

    async def fake_get_setting(db, key):
        return settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)

    client = _FakeClient()
    state = SimpleNamespace(ams_extruder_map=None, nozzles=[], kprofiles=[])
    monkeypatch.setattr("backend.app.services.printer_manager.printer_manager.get_client", lambda pid: client)
    monkeypatch.setattr("backend.app.services.printer_manager.printer_manager.get_status", lambda pid: state)
    return SimpleNamespace(settings=settings, client=client)


async def _legacy_row(db, *, rgba="000000FF", slicer_filament=""):
    """An ams_auto row minted before the tagless mint stamped the default's identity:
    no slicer_filament, no temps (prod spools 86 / 91 / 105 to the field)."""
    spool = Spool(
        material="PETG",
        subtype="HF",
        brand="Bambu Lab",
        rgba=rgba,
        label_weight=1000,
        weight_used=0,
        slicer_filament=slicer_filament,
        data_origin="ams_auto",
    )
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.commit()
    return spool


async def _publish(db, env, printer_id, spool, *, current_tray_info_idx="GFG99"):
    ok = await apply_spool_to_slot_via_mqtt(
        db=db,
        current_user=None,
        spool=spool,
        printer_id=printer_id,
        ams_id=0,
        tray_id=1,
        current_tray_info_idx=current_tray_info_idx,
        current_tray_type="PETG",
    )
    assert ok is True
    assert len(env.client.settings) == 1
    return env.client.settings[0]


class TestGenericIdSubstitutionAtTheWriteSite:
    async def test_legacy_row_publishes_the_tagless_default_never_generic(self, db_session, printer_factory, env):
        """INCIDENT PIN — the fallback-composed identity now routes through the same
        chokepoint the resolver uses: a fingerprint-matching row publishes the default's
        SPECIFIC id + its temps."""
        printer = await printer_factory()
        spool = await _legacy_row(db_session)

        wire = await _publish(db_session, env, printer.id, spool)

        assert wire["tray_info_idx"] == "GFG02"
        assert wire["setting_id"] == "GFSG02"
        assert (wire["nozzle_temp_min"], wire["nozzle_temp_max"]) == (230, 270)

    async def test_live_generic_on_the_wire_does_not_perpetuate_itself(self, db_session, printer_factory, env):
        """The slot already reading GFG99 was the self-perpetuating loop: that branch
        rejects generic values, falls through to the material fallback, and re-published
        the same id every ~30 s. It converges now."""
        printer = await printer_factory()
        spool = await _legacy_row(db_session)

        wire = await _publish(db_session, env, printer.id, spool, current_tray_info_idx="GFG99")

        assert wire["tray_info_idx"] == "GFG02"

    async def test_non_matching_row_keeps_the_generic_id(self, db_session, printer_factory, env):
        """Not a widening: a roll that does NOT fingerprint-match the default is not the
        default, and claiming its identity would be the same wrong answer in reverse."""
        printer = await printer_factory()
        spool = await _legacy_row(db_session, rgba="FF0000FF")  # red PETG

        wire = await _publish(db_session, env, printer.id, spool)

        assert wire["tray_info_idx"] == "GFG99"
        assert (wire["nozzle_temp_min"], wire["nozzle_temp_max"]) == (220, 260)  # MATERIAL_TEMPS

    async def test_feature_off_keeps_the_generic_id(self, db_session, printer_factory, env):
        """No configured tagless default → nothing to substitute; the slot still gets a
        usable generic identity rather than none."""
        env.settings["tagless_default_filament"] = ""
        printer = await printer_factory()
        spool = await _legacy_row(db_session)

        wire = await _publish(db_session, env, printer.id, spool)

        assert wire["tray_info_idx"] == "GFG99"

    async def test_row_with_a_stored_specific_id_is_untouched(self, db_session, printer_factory, env):
        """Control (prod spool 124, minted after the mint began stamping identity): the
        resolver answers, the fallback never runs."""
        printer = await printer_factory()
        spool = await _legacy_row(db_session, slicer_filament="GFG02")

        wire = await _publish(db_session, env, printer.id, spool)

        assert wire["tray_info_idx"] == "GFG02"
        assert (wire["nozzle_temp_min"], wire["nozzle_temp_max"]) == (230, 270)
