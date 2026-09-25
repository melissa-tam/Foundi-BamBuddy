"""The shop-air owner (``services/eject/shop_air``) on fixtures CUT from production.

The fixtures under ``fixtures/shop_air/`` are cut from the 7-day production export of
``/printer-sensor-history`` (60 s rows of value + target for nozzle, nozzle_2, bed and
chamber, printers 1-10, 2026-09-18 → 09-25 UTC; sensor values only):

* ``sensor_cuts.json`` — five single-printer windows, each the 95 minutes before its
  ``evaluated_at`` minute, chosen by running the qualification over the whole export and
  keeping the first minute of each SINGLE-cause outcome: an at-rest minute (printer 1,
  09-19 00:02, 29 °C), a minute refused only by a bed+nozzle target 88 min earlier
  (printer 3), one refused only by a NOZZLE-only target (printer 1, preheat), one refused
  only by the bed/chamber gap (printer 1, bed 26 vs chamber 28) and one refused only by
  drift (printer 1, a 1 °C step inside 20 min).
* ``week_samples.json`` — every at-rest sample ``derive_samples`` yields over the whole
  export (539 samples, all ten printers): the evidence base the estimate reads.

The user's caution these rules answer (2026-09-25): "a printer that was just running with
chamber fan stopping at x doesn't mean the shop air is x … consider day/night temps".
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from backend.app.models.printer import Printer
from backend.app.models.printer_sensor_history import PrinterSensorHistory
from backend.app.models.settings import Settings
from backend.app.models.shop_air_sample import ShopAirSample
from backend.app.services.eject import shop_air
from backend.app.services.eject.shop_air import (
    EjectLine,
    Sample,
    ShopAir,
    day_curve,
    derive_samples,
    eject_line_c,
    estimate,
    minutes_from_readings,
    own_air_c,
    qualify,
    release_limit,
    release_ok,
)

FIXTURES = Path(__file__).parent / "fixtures" / "shop_air"
SITE = ZoneInfo("America/Toronto")  # the farm's zone; the export's hours are EDT
KINDS = ("bed", "chamber", "nozzle", "nozzle_2")


def _cuts() -> dict[str, dict]:
    data = json.loads((FIXTURES / "sensor_cuts.json").read_text(encoding="utf-8"))
    return {cut["name"]: cut for cut in data["cuts"]}


def _readings(cut: dict) -> list[tuple[datetime, str, float | None, float | None]]:
    """The cut back in the recorder's long format — one row per kind per minute."""
    out = []
    for row in cut["rows"]:
        at = datetime.fromisoformat(row[0])
        for index, kind in enumerate(KINDS):
            value, target = row[1 + 2 * index], row[2 + 2 * index]
            if value is not None or target is not None:
                out.append((at, kind, value, target))
    return out


def _minutes(name: str, *, chamber_sensor: bool = True) -> list[shop_air.SensorMinute]:
    return minutes_from_readings(_readings(_cuts()[name]), chamber_sensor=chamber_sensor)


def _week() -> list[Sample]:
    data = json.loads((FIXTURES / "week_samples.json").read_text(encoding="utf-8"))
    return [Sample(printer_id=p, recorded_at=datetime.fromisoformat(t), value_c=v) for p, t, v in data["samples"]]


def _cleared(minutes):
    """The same evidence with every heater target erased — the single-cause control."""
    return [dataclasses.replace(minute, heating=False) for minute in minutes]


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


class TestFixtures:
    def test_the_fixtures_stay_small(self):
        """< 300 KB, sensor values only — the capsule's own bound on a fixture cut."""
        total = sum(path.stat().st_size for path in FIXTURES.iterdir())
        assert total < 300_000


# --------------------------------------------------------------------------- #
# Qualification
# --------------------------------------------------------------------------- #
class TestQualification:
    def test_an_at_rest_stretch_qualifies_at_the_lower_of_bed_and_chamber(self):
        minutes = _minutes("at_rest")
        last = minutes[-1]
        assert last.at == _at(_cuts()["at_rest"]["evaluated_at"])
        assert qualify(minutes, drying=False) == 29.0 == min(last.bed, last.chamber)

    def test_a_printer_within_90_min_of_a_bed_heater_does_not(self):
        """Flat, bed = chamber — and a bed+nozzle target 88 min before. Not the shop yet."""
        minutes = _minutes("bed_heat_within_90")
        heated = [m.at for m in minutes if m.heating]
        assert heated and minutes[-1].at - max(heated) < shop_air.REST_AFTER_HEAT
        assert qualify(minutes, drying=False) is None
        assert qualify(_cleared(minutes), drying=False) is not None  # the heat rule, and only it

    def test_any_heater_counts_a_nozzle_target_alone(self):
        """The rule is about ALL four heaters: here only the NOZZLE had a target (a preheat
        with the bed cold) and the minute is still refused."""
        cut = _cuts()["nozzle_heat_within_90"]
        heated_rows = [row for row in cut["rows"] if any((row[2 + 2 * i] or 0) > 0 for i in range(4))]
        last = heated_rows[-1]
        assert (last[6] or 0) > 0 and not (last[2] or 0) > 0  # nozzle target on, bed target off
        minutes = _minutes("nozzle_heat_within_90")
        assert qualify(minutes, drying=False) is None
        assert qualify(_cleared(minutes), drying=False) is not None

    def test_a_nozzle_2_target_counts_too(self):
        """The dual-nozzle H2C's second hotend is a heater like any other."""
        readings = _readings(_cuts()["at_rest"])
        halfway = readings[len(readings) // 2][0]
        readings.append((halfway, "nozzle_2", 25.0, 140.0))
        assert qualify(minutes_from_readings(readings, chamber_sensor=True), drying=False) is None

    def test_a_bed_chamber_gap_does_not(self):
        minutes = _minutes("bed_chamber_gap")
        last = minutes[-1]
        assert abs(last.bed - last.chamber) > shop_air.BED_CHAMBER_MAX_GAP_C
        assert not any(m.heating for m in minutes)
        assert qualify(minutes, drying=False) is None

    def test_drift_does_not(self):
        minutes = _minutes("drift")
        last = minutes[-1]
        flat = [m for m in minutes if m.at >= last.at - shop_air.FLAT_WINDOW]
        assert abs(last.bed - last.chamber) <= shop_air.BED_CHAMBER_MAX_GAP_C
        assert not any(m.heating for m in minutes)
        assert (
            max(m.bed for m in flat) - min(m.bed for m in flat) > shop_air.MAX_DRIFT_C
            or max(m.chamber for m in flat) - min(m.chamber for m in flat) > shop_air.MAX_DRIFT_C
        )
        assert qualify(minutes, drying=False) is None

    def test_a_drying_ams_does_not(self):
        assert qualify(_minutes("at_rest"), drying=True) is None

    def test_a_model_without_a_chamber_sensor_never_qualifies(self):
        """P1S/A1 report a meaningless chamber value; without the sensor there is no own air."""
        assert qualify(_minutes("at_rest", chamber_sensor=False), drying=False) is None

    def test_a_gap_in_the_evidence_is_heat_nobody_can_rule_out(self):
        minutes = _minutes("at_rest")
        middle = minutes[-1].at - timedelta(minutes=50)
        holed = [m for m in minutes if not (middle <= m.at < middle + timedelta(minutes=10))]
        assert qualify(holed, drying=False) is None

    def test_less_than_90_minutes_of_evidence_is_unknown_not_rest(self):
        minutes = _minutes("at_rest")
        short = [m for m in minutes if m.at >= minutes[-1].at - timedelta(minutes=60)]
        assert qualify(short, drying=False) is None


class TestDeriveSamples:
    @staticmethod
    def _flat(hours: int) -> list[shop_air.SensorMinute]:
        start = datetime(2026, 9, 20, 0, 0)
        return [shop_air.SensorMinute(start + timedelta(minutes=i), 28.0, 28.0, False) for i in range(hours * 60)]

    def test_at_most_one_sample_per_printer_per_ten_minutes(self):
        minutes = self._flat(3)
        samples = derive_samples(minutes, since=minutes[0].at)
        # The first minute whose evidence covers the 90-minute rest window (to within the
        # 2-minute tolerance), then every 10 minutes: minutes 88, 98, … 178 of 180.
        first = minutes[0].at + shop_air.REST_AFTER_HEAT - shop_air.EVIDENCE_TOLERANCE
        assert samples[0][0] == first
        gaps = {(b[0] - a[0]) for a, b in zip(samples, samples[1:], strict=False)}
        assert gaps == {shop_air.SAMPLE_EVERY}
        assert len(samples) == 10

    def test_the_throttle_is_seeded_by_the_last_stored_sample(self):
        minutes = self._flat(3)
        seeded = minutes[0].at + timedelta(minutes=95)
        samples = derive_samples(minutes, since=minutes[0].at, last_sample_at=seeded)
        assert samples[0][0] == seeded + shop_air.SAMPLE_EVERY

    def test_the_week_fixture_is_what_derive_samples_yields(self):
        """The estimate fixture IS the qualification's output — every at-rest minute of the
        at_rest cut appears in it, and all ten printers are thermometers somewhere."""
        week = _week()
        assert len(week) == 539
        assert {s.printer_id for s in week} == set(range(1, 11))
        evaluated = _at(_cuts()["at_rest"]["evaluated_at"])
        assert any(s.printer_id == 1 and s.recorded_at == evaluated and s.value_c == 29.0 for s in week)


# --------------------------------------------------------------------------- #
# Estimate
# --------------------------------------------------------------------------- #
class TestEstimate:
    def test_fresh_needs_three_printers_on_the_day_curve(self):
        shop = estimate(_week(), _at("2026-09-23T20:09:00"), site_tz=SITE)
        assert shop.basis == "fresh"
        assert shop.printers == 3
        assert shop.value_c == 27.0
        assert shop.as_of == _at("2026-09-23T20:09:00")

    def test_the_0921_dip_on_two_printers_is_not_the_shop(self):
        """2026-09-21 18:00 EDT: printers 6 and 8 read 22-23 °C (a door or the AC) while the
        rest of the fleet read 28. Two printers are an outlier, not the shop — not fresh,
        and the value is NOT the dip."""
        now = _at("2026-09-21T22:45:00")
        recent = [s for s in _week() if now - timedelta(minutes=60) <= s.recorded_at <= now]
        assert {s.printer_id for s in recent if s.value_c <= 23.0} == {6, 8}
        shop = estimate(_week(), now, site_tz=SITE)
        assert shop.basis != "fresh"
        assert shop.value_c == 28.0

    def test_carried_never_carries_the_dip(self):
        """At 23:00 the newest sample of all is printer 8's 23 °C: the carry skips it (the
        same outlier rule as fresh) and moves printer 7's 22:56 reading instead."""
        now = _at("2026-09-21T23:00:00")
        newest = max((s for s in _week() if s.recorded_at <= now), key=lambda s: s.recorded_at)
        assert (newest.printer_id, newest.value_c) == (8, 23.0)
        shop = estimate(_week(), now, site_tz=SITE)
        assert shop.basis == "carried"
        assert shop.as_of == _at("2026-09-21T22:56:00")
        assert shop.value_c >= 27.0

    def test_carried_across_a_busy_stretch_moves_along_the_day_curve(self):
        """No printer at rest from 09-19 19:59 UTC to 09-20 00:00 (a busy fleet): the last
        reading (printer 6, 28 °C at 15:59 EDT) is carried to 20:00 EDT by the curve's own
        hour-to-hour difference."""
        now = _at("2026-09-20T00:00:00")
        week = _week()
        assert not [s for s in week if _at("2026-09-19T19:59:00") < s.recorded_at <= now]
        shop = estimate(week, now, site_tz=SITE)
        assert shop.basis == "carried"
        assert shop.printers == 1
        assert shop.as_of == _at("2026-09-19T19:59:00")
        curve = day_curve([s for s in week if now - shop_air.HISTORY <= s.recorded_at <= now], site_tz=SITE)
        assert shop.value_c == pytest.approx(28.0 + curve[20] - curve[15])
        assert shop.value_c == 29.0

    def test_an_empty_hour_is_interpolated_from_its_neighbours(self):
        week = [
            s for s in _week() if s.recorded_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(SITE).hour not in (1, 2, 3)
        ]
        curve = day_curve(week, site_tz=SITE)
        assert curve[2] == pytest.approx(curve[0] + (curve[4] - curve[0]) * 2 / 4)

    def test_interpolation_runs_around_the_clock(self):
        """22:00-00:00 missing: 23:00 sits between 21:00 and 01:00, across midnight."""
        week = [
            s for s in _week() if s.recorded_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(SITE).hour not in (22, 23, 0)
        ]
        curve = day_curve(week, site_tz=SITE)
        assert curve[23] == pytest.approx(curve[21] + (curve[1] - curve[21]) * 2 / 4)
        assert curve[0] == pytest.approx(curve[21] + (curve[1] - curve[21]) * 3 / 4)

    def test_no_samples_is_unknown_and_there_is_no_line(self):
        shop = estimate([], _at("2026-09-23T20:09:00"), site_tz=SITE)
        assert shop == ShopAir.unknown()
        assert eject_line_c(shop, 2.0) is None

    def test_a_week_old_cache_is_unknown(self):
        assert estimate(_week(), _at("2026-10-05T00:00:00"), site_tz=SITE).basis == "unknown"

    def test_the_line_is_shop_air_plus_the_margin(self):
        assert eject_line_c(ShopAir(27.5, None, "fresh", 3), 2.0) == 29.5


# --------------------------------------------------------------------------- #
# The release predicate
# --------------------------------------------------------------------------- #
class TestReleasePredicate:
    @pytest.mark.parametrize(
        ("bed", "chamber", "line", "expected"),
        [
            (33.0, None, 33.0, True),  # at the line, no chamber term
            (33.1, None, 33.0, False),
            (31.0, 29.0, 26.0, True),  # own air + margin beats a low line
            (31.1, 29.0, 26.0, False),
            (33.0, 29.0, 33.0, True),  # the line beats a cool own air
            (33.1, 29.0, 33.0, False),
            (30.0, 28.0, None, True),  # unknown line: own air alone
            (30.5, 28.0, None, False),
            (20.0, None, None, False),  # neither term: cannot judge
            (None, 28.0, 33.0, False),  # no bed: cannot judge
        ],
    )
    def test_truth_table(self, bed, chamber, line, expected):
        assert release_ok(bed, chamber, line, 2.0) is expected

    def test_the_limit_names_the_term_that_set_it(self):
        assert release_limit(26.0, 29.0, 2.0) == shop_air.ReleaseLimit(31.0, "own_air")
        assert release_limit(33.0, 29.0, 2.0) == shop_air.ReleaseLimit(33.0, "line")
        assert release_limit(31.0, 29.0, 2.0) == shop_air.ReleaseLimit(31.0, "line")  # a tie reports the line
        assert release_limit(None, None, 2.0) is None

    @pytest.mark.parametrize(
        ("temperatures", "model", "expected"),
        [
            ({"chamber": 28.0}, "H2S", 28.0),
            ({"chamber": 28}, "O1C2", 28.0),
            ({"chamber": 28.0}, "P1S", None),  # a meaningless reading on a sensorless model
            ({"chamber": 28.0}, None, None),
            ({}, "H2S", None),
            ({"chamber": True}, "H2S", None),
            ({"chamber": None}, "H2S", None),
        ],
    )
    def test_own_air_is_the_chamber_of_a_model_with_a_real_sensor(self, temperatures, model, expected):
        assert own_air_c(temperatures, model=model) == expected


# --------------------------------------------------------------------------- #
# The database half — one evidence base for the live writer and the backfill
# --------------------------------------------------------------------------- #
async def _printer(db, name="AIR", model="H2S") -> Printer:
    printer = Printer(name=name, serial_number=f"SA{name}", ip_address="10.0.0.9", access_code="x", model=model)
    db.add(printer)
    await db.flush()
    return printer


async def _seed_history(db, printer_id: int, readings) -> None:
    for at, kind, value, target in readings:
        db.add(
            PrinterSensorHistory(printer_id=printer_id, sensor_kind=kind, value=value, target=target, recorded_at=at)
        )
    await db.commit()


def _extend_flat(readings, minutes: int):
    """Continue the cut's last minute, unchanged, for ``minutes`` more minutes."""
    last_at = readings[-1][0]
    tail = [r for r in readings if r[0] == last_at]
    return [
        (at + timedelta(minutes=step), kind, value, target)
        for step in range(1, minutes + 1)
        for at, kind, value, target in tail
    ]


class TestLiveWriter:
    @pytest.fixture(autouse=True)
    def _model(self, monkeypatch):
        models = {"model": "H2S"}
        monkeypatch.setattr(shop_air.printer_manager, "get_model", lambda printer_id: models["model"])
        return models

    async def test_the_minute_just_written_is_sampled(self, db_session):
        printer = await _printer(db_session)
        cut = _cuts()["at_rest"]
        await _seed_history(db_session, printer.id, _readings(cut))
        now = _at(cut["evaluated_at"]) + timedelta(seconds=30)

        assert await shop_air.note_reading(db_session, printer.id, now) == 29.0
        await db_session.commit()

        rows = (await db_session.execute(select(ShopAirSample))).scalars().all()
        assert [(r.printer_id, r.recorded_at, r.value_c, r.rule_version) for r in rows] == [
            (printer.id, _at(cut["evaluated_at"]), 29.0, shop_air.RULE_VERSION)
        ]

    async def test_at_most_one_sample_per_ten_minutes(self, db_session):
        printer = await _printer(db_session)
        cut = _cuts()["at_rest"]
        readings = _readings(cut)
        await _seed_history(db_session, printer.id, readings + _extend_flat(readings, 10))
        evaluated = _at(cut["evaluated_at"])

        assert await shop_air.note_reading(db_session, printer.id, evaluated) == 29.0
        await db_session.commit()
        assert await shop_air.note_reading(db_session, printer.id, evaluated + timedelta(minutes=9)) is None
        assert await shop_air.note_reading(db_session, printer.id, evaluated + timedelta(minutes=10)) == 29.0
        await db_session.commit()
        count = len((await db_session.execute(select(ShopAirSample))).scalars().all())
        assert count == 2

    async def test_a_drying_ams_is_not_sampled(self, db_session, monkeypatch):
        printer = await _printer(db_session)
        cut = _cuts()["at_rest"]
        await _seed_history(db_session, printer.id, _readings(cut))
        monkeypatch.setattr(shop_air, "_drying_now", lambda printer_id: True)
        assert await shop_air.note_reading(db_session, printer.id, _at(cut["evaluated_at"])) is None

    async def test_a_sensorless_model_is_never_sampled(self, db_session, _model):
        printer = await _printer(db_session, model="P1S")
        _model["model"] = "P1S"
        cut = _cuts()["at_rest"]
        await _seed_history(db_session, printer.id, _readings(cut))
        assert await shop_air.note_reading(db_session, printer.id, _at(cut["evaluated_at"])) is None

    async def test_the_backfill_reads_the_same_evidence_the_same_way(self, db_session):
        """ONE evidence base: the bootstrap backfill over the same history yields exactly the
        sample the live writer wrote for that minute."""
        printer = await _printer(db_session)
        cut = _cuts()["at_rest"]
        await _seed_history(db_session, printer.id, _readings(cut))
        evaluated = _at(cut["evaluated_at"])
        live = await shop_air.note_reading(db_session, printer.id, evaluated)
        await db_session.commit()

        count = await shop_air.backfill(await db_session.connection(), now=evaluated)
        await db_session.commit()

        rows = (await db_session.execute(select(ShopAirSample))).scalars().all()
        assert count == 1
        assert [(r.recorded_at, r.value_c) for r in rows] == [(evaluated, live)]


class TestPruneAndRead:
    async def test_prune_follows_the_history_cutoff_and_printer_scope(self, db_session):
        a = await _printer(db_session, "PA")
        b = await _printer(db_session, "PB")
        now = datetime(2026, 9, 25, 12, 0)
        for printer in (a, b):
            for days in (40, 10):
                db_session.add(
                    ShopAirSample(
                        printer_id=printer.id,
                        recorded_at=now - timedelta(days=days),
                        value_c=28.0,
                        rule_version=shop_air.RULE_VERSION,
                    )
                )
        await db_session.commit()

        assert await shop_air.prune(db_session, before=now - timedelta(days=30), printer_id=a.id) == 1
        assert await shop_air.prune(db_session, before=now - timedelta(days=30)) == 1
        await db_session.commit()
        left = (await db_session.execute(select(ShopAirSample.printer_id))).scalars().all()
        assert sorted(left) == sorted([a.id, b.id])

    async def test_current_line_reads_the_cache_and_the_margin(self, db_session, monkeypatch):
        """The DB loader is the pure estimate over the cache, plus the one margin setting."""
        monkeypatch.setenv("TZ", "America/Toronto")
        printers = {}
        for sample in _week():
            if sample.printer_id not in printers:
                printers[sample.printer_id] = await _printer(db_session, f"W{sample.printer_id}")
            db_session.add(
                ShopAirSample(
                    printer_id=printers[sample.printer_id].id,
                    recorded_at=sample.recorded_at,
                    value_c=sample.value_c,
                    rule_version=shop_air.RULE_VERSION,
                )
            )
        db_session.add(Settings(key="farm_cooldown_margin_c", value="2.5"))
        await db_session.commit()

        line = await shop_air.current_line(db_session, _at("2026-09-23T20:09:00"))

        assert line == EjectLine(shop=ShopAir(27.0, _at("2026-09-23T20:09:00"), "fresh", 3), margin_c=2.5, line_c=29.5)

    async def test_the_margin_falls_back_to_the_schema_default(self, db_session):
        assert await shop_air.read_margin_c(db_session) == 2.0
