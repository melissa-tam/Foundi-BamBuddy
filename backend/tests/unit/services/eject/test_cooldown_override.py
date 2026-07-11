"""Unit tests for the per-run cooldown override on the eject pipeline (Phase 2).

Covers the shared ``resolve_cooldown_override`` helper and the fact that BOTH
the dispatch block generation AND the cooldown monitor's release threshold key
off the same override — so the in-file ``M190 R`` wait and the server-side gate
never disagree (incident PCO-M18-2904 BUG B)."""

import contextlib

import pytest

from backend.app.models.eject_profile import EjectProfile
from backend.app.services.eject.generator import generate_eject_gcode
from backend.app.services.eject.validator import validate_eject_gcode
from backend.tests.unit.services.eject.geometry_fixtures import H2S_GEOMETRY

_PROFILE_DEFAULTS = {
    "cooldown_temp_c": 28.0,
    "cooldown_retries": 5,
    "clearance_mm": 10.0,
    "z_offset_mm": 0.4,
    "descent_steps": 4,
    "x_passes": 11,
    "x_margin_mm": 3.0,
    "front_overhang_mm": 2.0,
    "back_overhang_mm": 2.0,
    "eject_speed_mm_min": 3000,
    "skim_speed_mm_min": 1500,
    "cooling_fan_assist": True,
    "max_part_height_mm": 60.0,
}


def _profile(**overrides) -> EjectProfile:
    defaults = {"name": "override", **_PROFILE_DEFAULTS}
    defaults.update(overrides)
    profile = EjectProfile()
    for key, value in defaults.items():
        setattr(profile, key, value)
    return profile


async def _add_profile(db_session, **overrides) -> EjectProfile:
    defaults = {"name": "resolve", **_PROFILE_DEFAULTS}
    defaults.update(overrides)
    profile = EjectProfile(**defaults)
    db_session.add(profile)
    await db_session.commit()
    await db_session.refresh(profile)
    return profile


async def _add_batch(db_session, override):
    from backend.app.models.print_batch import PrintBatch

    batch = PrintBatch(name="run", quantity=1, status="active", cooldown_temp_c_override=override)
    db_session.add(batch)
    await db_session.commit()
    await db_session.refresh(batch)
    return batch


class TestCooldownOverride:
    def test_none_uses_profile_value(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY, cooldown_temp_c=None)
        assert gcode.count("M190 R28") == 5

    def test_override_changes_emitted_threshold(self):
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY, cooldown_temp_c=22.0)
        assert gcode.count("M190 R22") == 5
        assert "M190 R28" not in gcode

    def test_generate_and_validate_share_override(self):
        # The generated block validates only when the validator is told the same
        # effective temp — proving generation + validation stay consistent.
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY, cooldown_temp_c=22.0)
        ok = validate_eject_gcode(gcode, _profile(), 30.0, H2S_GEOMETRY, cooldown_temp_c=22.0)
        assert ok.ok, ok.errors

    def test_validator_flags_mismatched_threshold(self):
        # Block emitted at 22 but validated against the profile default (28) fails.
        gcode = generate_eject_gcode(_profile(), 30.0, H2S_GEOMETRY, cooldown_temp_c=22.0)
        result = validate_eject_gcode(gcode, _profile(), 30.0, H2S_GEOMETRY, cooldown_temp_c=None)
        assert not result.ok
        assert any("threshold" in e for e in result.errors)


@pytest.mark.asyncio
class TestResolveCooldownOverrideHelper:
    """The shared helper dispatch + monitor both call."""

    async def test_none_batch_returns_none(self, db_session):
        from backend.app.services.eject.dispatch import resolve_cooldown_override

        assert await resolve_cooldown_override(db_session, None) is None

    async def test_returns_override_when_run_sets_it(self, db_session):
        from backend.app.services.eject.dispatch import resolve_cooldown_override

        batch = await _add_batch(db_session, 34.0)
        assert await resolve_cooldown_override(db_session, batch.id) == 34.0

    async def test_returns_none_when_run_has_no_override(self, db_session):
        from backend.app.services.eject.dispatch import resolve_cooldown_override

        batch = await _add_batch(db_session, None)
        assert await resolve_cooldown_override(db_session, batch.id) is None


@pytest.mark.asyncio
class TestMonitorThresholdHonorsOverride:
    """`monitor._resolve_eject_threshold` must return the run override when set,
    else the profile's cooldown_temp_c — matching what the eject block was
    generated with (BUG B: the monitor previously ignored the override)."""

    async def _resolve(self, db_session, monkeypatch, *, batch_id):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.eject import monitor as monitor_mod

        @contextlib.asynccontextmanager
        async def _fake_session():
            yield db_session

        monkeypatch.setattr("backend.app.core.database.async_session", _fake_session, raising=False)

        # Phase 1: the threshold resolver keys off the SPECIFIC finished item
        # (db.get), not the most-recently-started item. Seed the real row.
        item = PrintQueueItem(
            printer_id=7,
            eject_profile_id=self._profile_id,
            batch_id=batch_id,
            first_article=False,
            status="printing",
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)
        return await monitor_mod._resolve_eject_threshold(item.id)

    async def test_override_present_resolves_override(self, db_session, monkeypatch):
        profile = await _add_profile(db_session, name="mon-ovr", cooldown_temp_c=33.0)
        self._profile_id = profile.id
        batch = await _add_batch(db_session, 34.0)
        threshold = await self._resolve(db_session, monkeypatch, batch_id=batch.id)
        assert threshold == 34.0  # run override wins over the profile's 33

    async def test_override_absent_resolves_profile_value(self, db_session, monkeypatch):
        profile = await _add_profile(db_session, name="mon-prof", cooldown_temp_c=33.0)
        self._profile_id = profile.id
        batch = await _add_batch(db_session, None)
        threshold = await self._resolve(db_session, monkeypatch, batch_id=batch.id)
        assert threshold == 33.0  # falls back to the profile's cooldown_temp_c

    async def test_no_batch_resolves_profile_value(self, db_session, monkeypatch):
        profile = await _add_profile(db_session, name="mon-nobatch", cooldown_temp_c=33.0)
        self._profile_id = profile.id
        threshold = await self._resolve(db_session, monkeypatch, batch_id=None)
        assert threshold == 33.0
