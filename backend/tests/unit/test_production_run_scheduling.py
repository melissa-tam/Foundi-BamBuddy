"""Unit tests for the production-run deferred-start time helpers (Phase 5).

``_as_utc`` / ``resolve_scheduled_start`` normalise an operator-supplied start
time to the naive-UTC convention of ``PrintQueueItem.scheduled_time`` and collapse
a null/past time to ASAP. Pure functions — no DB.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.production_run import _as_utc, resolve_scheduled_start


@pytest.mark.unit
class TestAsUtc:
    def test_none_passthrough(self):
        assert _as_utc(None) is None

    def test_aware_coerced_to_naive_utc(self):
        # 12:00 in a +02:00 zone == 10:00 UTC, stored naive.
        aware = datetime(2030, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        out = _as_utc(aware)
        assert out == datetime(2030, 1, 1, 10, 0)
        assert out.tzinfo is None

    def test_naive_assumed_utc_unchanged(self):
        naive = datetime(2030, 1, 1, 9, 30)
        assert _as_utc(naive) == naive


@pytest.mark.unit
class TestResolveScheduledStart:
    def test_none_is_asap(self):
        assert resolve_scheduled_start(None) is None

    def test_future_is_kept_as_naive_utc(self):
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        out = resolve_scheduled_start(future)
        assert out is not None
        assert out.tzinfo is None
        assert out > datetime.now(timezone.utc).replace(tzinfo=None)

    def test_past_collapses_to_asap(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        assert resolve_scheduled_start(past) is None

    def test_naive_future_is_kept(self):
        future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        assert resolve_scheduled_start(future) is not None
