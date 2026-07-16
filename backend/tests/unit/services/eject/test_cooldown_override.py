"""Unit tests for the per-run cooldown override on the eject pipeline (Phase 2).

Covers the shared ``resolve_cooldown_override`` helper and the fact that the
cooldown MONITOR's server-side release threshold keys off the same override — so
the value that gates dispatch matches the run's intent (incident PCO-M18-2904
BUG B). The eject block is motion-only now, so the override no longer affects the
G-code — only the monitor's release threshold."""

import contextlib

import pytest

from backend.app.models.eject_profile import EjectProfile

_PROFILE_DEFAULTS = {
    "cooldown_temp_c": 28.0,
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
