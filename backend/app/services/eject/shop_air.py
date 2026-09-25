"""Shop air — the air the farm's plates cool into, MEASURED, and the ONE eject line derived from it.

**Why this module exists.** Until 2026-09-25 the eject line was a number an operator typed:
``eject_profiles.cooldown_temp_c`` = 33 °C on all three production profiles, overridable per
run (``print_batches.cooldown_temp_c_override``) and policed by a UI-only "ambient trap"
warning (``farm_cooldown_warn_floor_c``) — one value stored three times, and a SUMMER number.
A line at or below the air the bed sits in never releases (the stall the warning existed to
catch); a line well above it ejects a warm part. Nothing measured the shop. User ruling: ONE
value — the eject line is MEASURED shop air plus ONE margin setting
(``farm_cooldown_margin_c``), and all three stored copies are deleted.

**What counts as shop air (user caution, 2026-09-25):** "a printer that was just running with
chamber fan stopping at x doesn't mean the shop air is x (ambient raising it a few degrees) …
consider day/night temps". Production sensor history agrees: in a cooldown the chamber air
settles at 26-27 °C within ~15-20 min while the bed is still ~35 °C and falling ~0.5 °C/min, so
a chamber reading taken off a printer that just finished is its OWN warmed air, not the room's.
The only printer that is a thermometer is one AT REST, and :func:`qualify` is the one
definition of that (all four conditions over ``printer_sensor_history``, the one evidence base):

* no heater target > 0 — nozzle, nozzle_2, bed OR chamber — in the preceding
  :data:`REST_AFTER_HEAT` (90 min), with the evidence actually COVERING that window (a gap in
  the history is heat nobody can rule out);
* no AMS drying cycle running on that printer;
* bed and chamber within :data:`BED_CHAMBER_MAX_GAP_C` (1.5 °C) of each other — a bed still
  warmer than its air is still cooling;
* each of bed and chamber flat — drifting ≤ :data:`MAX_DRIFT_C` (0.5 °C) — over
  :data:`FLAT_WINDOW` (20 min).

The value is the LOWER of bed and chamber. **Honest label: this measures IDLE-ENCLOSURE air**
— the air inside a closed printer whose electronics stay powered — which is an UPPER bound on
room air (standing electronics heat, model-dependent). It errs WARM, and warm is the safe
direction: a warm estimate puts the line higher, a higher line releases sooner, and no error
in this direction can hold a bed forever above a line it cannot reach.

**The estimate follows the day** (:func:`estimate`). Three bases, in order:

1. ``fresh`` — at-rest samples from ≥ :data:`FRESH_MIN_PRINTERS` (3) printers in the last
   :data:`FRESH_WINDOW` (60 min), each within :data:`FRESH_CURVE_TOLERANCE_C` (3 °C) of the day
   curve at its own hour → their median. A door or an AC draught on one or two printers is an
   outlier, not the shop: on 2026-09-21 at 18:00 EDT two printers read 22-23 °C while the
   fleet read 28.
2. ``carried`` — otherwise the latest sample (≤ :data:`HISTORY`, 7 days) that sits on its
   own hour's curve (the same outlier rule — a dip is not the shop on either basis) moved
   to the current hour along the DAY CURVE: the median at-rest reading per site-local hour
   (``utils/site_time``) over the last 7 days, an hour with no samples interpolated from its
   neighbours around the clock, applied as the hour-to-hour DIFFERENCE so a day that runs
   warmer or cooler than the week keeps its own level. A fully busy fleet has no printer at
   rest for hours, which is why this is the common basis in production.
3. ``unknown`` — no samples at all. The line is then None, never a number invented from a
   hot chamber.

**The release predicate** (:func:`release_ok`) is ``bed ≤ max(line, own_chamber + margin)``.
A bed can never go below the air it sits in, so once it is within the margin of its OWN air
further waiting buys nothing; and because the bed out-lags the exhausted chamber air all through
a cooldown, the own-air term only fires near equilibrium. A low fleet estimate therefore can
never strand a printer, and a warm one never makes it wait past its own floor.

**The 7-day production replay** (10 printers, 2026-09-18 → 09-25, scratch ``shop_air_sim2.py``
over the ``/printer-sensor-history`` export): 542 at-rest samples on all ten printers (540 under
this module's rule, which also counts ``nozzle_2`` and refuses an evidence gap); day curve
27-29 °C EDT; 274 cooldowns → basis fresh 19 / carried 247 / unknown 8; at margin 2 the line's
median is 30 °C and the release median 31 °C (28-35); cooldowns reaching the plateau path
61 → 32; added minutes against the old 33 °C line median 6.4, p90 11.9.

**The sample cache.** ``shop_air_sample`` (:class:`~backend.app.models.shop_air_sample.ShopAirSample`)
is a documented derived cache over the sensor history, written ONLY here: live by
:func:`note_reading` right after each minute's history write, and at bootstrap by
:func:`backfill` over the last 7 days through the same :func:`qualify`. Pruned with the history
it came from (:func:`prune`).
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Literal

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.printer_sensor_history import SENSOR_KINDS, PrinterSensorHistory
from backend.app.models.shop_air_sample import ShopAirSample
from backend.app.schemas.settings import AppSettings
from backend.app.services import ams_presence
from backend.app.services.printer_manager import printer_manager, supports_chamber_temp
from backend.app.utils.site_time import site_zone, to_site

logger = logging.getLogger(__name__)

# The qualification rule's version. Bump it whenever :func:`qualify` would answer a stored
# minute differently: the bootstrap migration then drops the whole cache and re-derives it
# from the sensor history, so samples measured under two rules are never mixed.
RULE_VERSION = 1

# -- the at-rest qualification ------------------------------------------------------------
# A heater that ran inside this window may still be warming the enclosure (the user's caution:
# a printer that just ran is not a thermometer). 90 min is past the whole measured cooldown —
# production median 63 min natural convection, 44 min with fan + hold.
REST_AFTER_HEAT = timedelta(minutes=90)
# Both readings flat over this window: a bed still sliding toward its air is not at rest.
FLAT_WINDOW = timedelta(minutes=20)
BED_CHAMBER_MAX_GAP_C = 1.5
MAX_DRIFT_C = 0.5
# Evidence rules. The recorder writes every 60 s and routinely skips one minute boundary
# (2-minute gaps, ~24 a week per printer on the production export); a longer hole is time the
# farm did not see, so a heater it cannot rule out. The tolerance is how close to the window's
# start the earliest reading must sit for the window to count as covered.
MAX_EVIDENCE_GAP = timedelta(minutes=5)
EVIDENCE_TOLERANCE = timedelta(minutes=2)
# At most one sample per printer per this long: a printer at rest for a day would otherwise
# dominate every median with 1440 near-identical votes.
SAMPLE_EVERY = timedelta(minutes=10)

# -- the estimate -------------------------------------------------------------------------
FRESH_WINDOW = timedelta(minutes=60)
FRESH_MIN_PRINTERS = 3
FRESH_CURVE_TOLERANCE_C = 3.0
# ONE horizon for the day curve, the carried sample's age limit and the bootstrap backfill:
# the cache must hold at least what the estimate reads, and the estimate reads a week so the
# day curve sees every hour on more than one day.
HISTORY = timedelta(days=7)

MARGIN_SETTING = "farm_cooldown_margin_c"

Basis = Literal["fresh", "carried", "unknown"]
ReleaseTerm = Literal["line", "own_air"]


def _utcnow() -> datetime:
    """Naive UTC — the fork's stored-timestamp convention (``utils/site_time``)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Qualification — pure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SensorMinute:
    """One recorded minute of one printer, as the qualification reads it.

    ``heating`` is "ANY heater had a target above 0 this minute" — the four kinds collapse to
    one flag because the rule asks about heat, not about which heater. ``chamber`` is None on a
    model without a real chamber sensor (P1P / P1S / A1 report a meaningless
    ``chamber_temper``, which the recorder stores anyway), which makes such a printer
    structurally unable to qualify rather than able to qualify on noise.
    """

    at: datetime
    bed: float | None
    chamber: float | None
    heating: bool


def minutes_from_readings(
    readings: Iterable[tuple[datetime, str, float | None, float | None]], *, chamber_sensor: bool
) -> list[SensorMinute]:
    """Fold long-format history rows ``(recorded_at, sensor_kind, value, target)`` into minutes.

    The recorder writes one row per kind per tick, each with its own server timestamp, so the
    minute (truncated) is the unit that brings a tick's four rows back together. Ordered by
    time. ``chamber_sensor`` False discards the chamber reading (see :class:`SensorMinute`).
    """
    by_minute: dict[datetime, list] = {}
    for recorded_at, kind, value, target in readings:
        minute = recorded_at.replace(second=0, microsecond=0)
        slot = by_minute.setdefault(minute, [None, None, False])
        if target is not None and target > 0:
            slot[2] = True
        if kind == "bed":
            slot[0] = value
        elif kind == "chamber" and chamber_sensor:
            slot[1] = value
    return [SensorMinute(at, bed, chamber, heating) for at, (bed, chamber, heating) in sorted(by_minute.items())]


def qualify(minutes: Sequence[SensorMinute], *, drying: bool) -> float | None:
    """Is the LAST minute of ``minutes`` a shop-air reading? Its value, or None.

    THE one definition of "this printer is at rest" (module docstring), evaluated at
    ``minutes[-1].at`` over the evidence before it; the live writer and the bootstrap backfill
    both call exactly this. ``minutes`` must be time-ordered and reach back at least
    :data:`REST_AFTER_HEAT` — less evidence than that is not "no heat", it is "unknown", and
    answers None. ``drying`` is the one input the sensor history cannot carry (the live writer
    reads it off the printer; the backfill cannot know it and passes False).

    Cheapest refusals first, and the backward walk stops at the first heated minute, so a
    printing printer costs one comparison per call.
    """
    if drying or not minutes:
        return None
    last = minutes[-1]
    if last.heating or last.bed is None or last.chamber is None:
        return None
    if abs(last.bed - last.chamber) > BED_CHAMBER_MAX_GAP_C:
        return None

    rest_from = last.at - REST_AFTER_HEAT
    flat_from = last.at - FLAT_WINDOW
    newer = last.at
    earliest = last.at
    earliest_flat = last.at
    beds: list[float] = []
    chambers: list[float] = []
    for minute in reversed(minutes):
        if minute.at < rest_from:
            break
        if newer - minute.at > MAX_EVIDENCE_GAP or minute.heating:
            return None
        if minute.at >= flat_from:
            if minute.bed is None or minute.chamber is None:
                return None
            beds.append(minute.bed)
            chambers.append(minute.chamber)
            earliest_flat = minute.at
        newer = minute.at
        earliest = minute.at

    if earliest > rest_from + EVIDENCE_TOLERANCE or earliest_flat > flat_from + EVIDENCE_TOLERANCE:
        return None
    if max(beds) - min(beds) > MAX_DRIFT_C or max(chambers) - min(chambers) > MAX_DRIFT_C:
        return None
    return min(last.bed, last.chamber)


def derive_samples(
    minutes: Sequence[SensorMinute], *, since: datetime, last_sample_at: datetime | None = None
) -> list[tuple[datetime, float]]:
    """Every sample :func:`qualify` yields over a minute series, at most one per :data:`SAMPLE_EVERY`.

    The backfill's walk: each minute at or after ``since`` is evaluated over the evidence
    before it, exactly as the live writer evaluates the minute it has just written.
    ``last_sample_at`` seeds the throttle. Returns ``(minute, value)`` pairs, time-ordered.
    """
    out: list[tuple[datetime, float]] = []
    start = 0
    for index, minute in enumerate(minutes):
        if minute.at < since:
            continue
        if last_sample_at is not None and minute.at - last_sample_at < SAMPLE_EVERY:
            continue
        while minutes[start].at < minute.at - REST_AFTER_HEAT - MAX_EVIDENCE_GAP:
            start += 1
        value = qualify(minutes[start : index + 1], drying=False)
        if value is not None:
            out.append((minute.at, value))
            last_sample_at = minute.at
    return out


# --------------------------------------------------------------------------- #
# Estimate — pure
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Sample:
    """One cached at-rest reading, as the estimate reads it."""

    printer_id: int
    recorded_at: datetime
    value_c: float


@dataclass(frozen=True)
class ShopAir:
    """The shop air right now, and how it is known.

    ``as_of`` is the newest contributing sample for ``fresh`` and the carried sample's own
    minute for ``carried`` (naive UTC); ``printers`` is how many printers the value rests on
    (the fresh quorum, 1 when carried, 0 when unknown).
    """

    value_c: float | None
    as_of: datetime | None
    basis: Basis
    printers: int

    @classmethod
    def unknown(cls) -> ShopAir:
        return cls(value_c=None, as_of=None, basis="unknown", printers=0)


def _site_hour(at: datetime, site_tz: tzinfo | None) -> int:
    return to_site(at, site_tz).hour


def day_curve(samples: Sequence[Sample], *, site_tz: tzinfo | None) -> dict[int, float] | None:
    """The median at-rest reading per site-local hour, every hour filled; None with no samples.

    An hour with no samples (a busy stretch that recurs every day) is interpolated linearly
    between its nearest known neighbours AROUND THE CLOCK — 23:00 sits between 22:00 and
    00:00 — so the night is never extrapolated off one edge of the day.
    """
    by_hour: dict[int, list[float]] = {}
    for sample in samples:
        by_hour.setdefault(_site_hour(sample.recorded_at, site_tz), []).append(sample.value_c)
    if not by_hour:
        return None
    known = {hour: statistics.median(values) for hour, values in by_hour.items()}
    hours = sorted(known)
    curve: dict[int, float] = {}
    for hour in range(24):
        if hour in known:
            curve[hour] = known[hour]
            continue
        before = max((h for h in hours if h < hour), default=hours[-1] - 24)
        after = min((h for h in hours if h > hour), default=hours[0] + 24)
        low, high = known[before % 24], known[after % 24]
        curve[hour] = low + (high - low) * (hour - before) / (after - before)
    return curve


def estimate(samples: Sequence[Sample], now: datetime, *, site_tz: tzinfo | None) -> ShopAir:
    """The shop air at ``now`` (naive UTC) from cached samples. Pure — see the module docstring.

    Only samples inside ``[now - HISTORY, now]`` are read, so the caller may pass more.
    """
    window = [sample for sample in samples if now - HISTORY <= sample.recorded_at <= now]
    curve = day_curve(window, site_tz=site_tz)
    if curve is None:
        return ShopAir.unknown()

    def on_curve(sample: Sample) -> bool:
        return abs(sample.value_c - curve[_site_hour(sample.recorded_at, site_tz)]) <= FRESH_CURVE_TOLERANCE_C

    inliers = [sample for sample in window if on_curve(sample)]
    recent = [sample for sample in inliers if sample.recorded_at >= now - FRESH_WINDOW]
    quorum = {sample.printer_id for sample in recent}
    if len(quorum) >= FRESH_MIN_PRINTERS:
        return ShopAir(
            value_c=statistics.median(sample.value_c for sample in recent),
            as_of=max(sample.recorded_at for sample in recent),
            basis="fresh",
            printers=len(quorum),
        )

    # The carried sample obeys the same outlier rule as a fresh one: a dip is not the shop
    # whichever basis would read it. On the 7-day replay 6 of 271 carried arms would
    # otherwise have carried the 09-21 22-23 °C dip, a 25 °C line. Only when no sample in
    # the week sits on its hour's curve (a degenerate, two-sample week) is the latest
    # sample carried regardless, because the estimate is total over any non-empty week.
    latest = max(inliers or window, key=lambda sample: sample.recorded_at)
    shift = curve[_site_hour(now, site_tz)] - curve[_site_hour(latest.recorded_at, site_tz)]
    return ShopAir(value_c=latest.value_c + shift, as_of=latest.recorded_at, basis="carried", printers=1)


# --------------------------------------------------------------------------- #
# The line and the release predicate — pure
# --------------------------------------------------------------------------- #
def eject_line_c(shop: ShopAir, margin_c: float) -> float | None:
    """THE eject line: shop air + the ONE margin, or None when shop air is unknown."""
    return None if shop.value_c is None else shop.value_c + margin_c


@dataclass(frozen=True)
class ReleaseLimit:
    """The temperature a bed releases at right now, and which term set it (for the log)."""

    value_c: float
    term: ReleaseTerm


def release_limit(line_c: float | None, chamber_c: float | None, margin_c: float) -> ReleaseLimit | None:
    """``max(line, chamber + margin)`` over the terms that exist; None when neither does.

    The explanation of :func:`release_ok` — what a log line or a hot-bed confirm reports as
    "the threshold" — never a second decision. A tie reports the line.
    """
    own_air = None if chamber_c is None else chamber_c + margin_c
    if own_air is None and line_c is None:
        return None
    if own_air is None or (line_c is not None and line_c >= own_air):
        return ReleaseLimit(value_c=line_c, term="line")
    return ReleaseLimit(value_c=own_air, term="own_air")


def release_ok(bed_c: float | None, chamber_c: float | None, line_c: float | None, margin_c: float) -> bool:
    """THE release predicate: ``bed ≤ max(line, chamber + margin)``. One function, every caller.

    A bed can never go below the air it sits in, so once it is within the margin of its OWN
    air further waiting buys nothing — and because the bed out-lags the exhausted chamber air
    all through a cooldown (production: chamber 26-27 °C within ~15-20 min, bed still ~35 °C),
    the own-air term only fires near equilibrium. A missing term is absent. No bed, or no term
    at all, is False: the predicate cannot judge, and the cooldown watch's plateau and max-hold
    cap are what end such a wait.
    """
    limit = release_limit(line_c, chamber_c, margin_c)
    return bed_c is not None and limit is not None and bed_c <= limit.value_c


def own_air_c(temperatures: Mapping[str, object], *, model: str | None) -> float | None:
    """The printer's OWN air right now — its chamber reading — or None.

    None on a model with no real chamber sensor (``printer_manager.supports_chamber_temp``:
    P1P / P1S / A1 report a meaningless ``chamber_temper``), and on a missing or non-numeric
    reading. The one reading of the own-air term for every caller of :func:`release_ok`.
    """
    if not supports_chamber_temp(model):
        return None
    value = temperatures.get("chamber")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# --------------------------------------------------------------------------- #
# The database half
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EjectLine:
    """The line as one read: the shop air it came from, the margin, and the line itself."""

    shop: ShopAir
    margin_c: float
    line_c: float | None


_MARGIN_DEFAULT = float(AppSettings.model_fields[MARGIN_SETTING].default)


async def read_margin_c(db: AsyncSession) -> float:
    """``farm_cooldown_margin_c``, falling back to the schema default (its one origin)."""
    from backend.app.api.routes.settings import get_setting

    raw = await get_setting(db, MARGIN_SETTING)
    if raw is None:
        return _MARGIN_DEFAULT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _MARGIN_DEFAULT


async def current_shop_air(db: AsyncSession, now: datetime | None = None) -> ShopAir:
    """The shop air at ``now`` (naive UTC; default the present) from the sample cache."""
    now = now if now is not None else _utcnow()
    rows = (
        await db.execute(
            select(ShopAirSample.printer_id, ShopAirSample.recorded_at, ShopAirSample.value_c).where(
                ShopAirSample.recorded_at >= now - HISTORY, ShopAirSample.recorded_at <= now
            )
        )
    ).all()
    samples = [Sample(printer_id=pid, recorded_at=at, value_c=value) for pid, at, value in rows]
    return estimate(samples, now, site_tz=site_zone())


async def current_line(db: AsyncSession, now: datetime | None = None) -> EjectLine:
    """The eject line right now — THE read every consumer of the line makes.

    The cooldown watch (once, at arm), the manual-eject and first-article hot-bed gates, the
    foreign-plate page and ``GET /shop-air`` all come here, so none of them can hold a second
    idea of the line.
    """
    shop = await current_shop_air(db, now)
    margin = await read_margin_c(db)
    return EjectLine(shop=shop, margin_c=margin, line_c=eject_line_c(shop, margin))


async def arm_line() -> EjectLine:
    """:func:`current_line` in its own session, for the cooldown watch's arm. Never raises.

    A watch that fails to arm strands the plate gate (2026-07-18/07-21), so a read failure
    arms with shop air UNKNOWN and the schema's margin: the own-air term, the plateau and the
    max-hold cap still end the wait.
    """
    from backend.app.core.database import async_session

    try:
        async with async_session() as db:
            return await current_line(db)
    except Exception:  # noqa: BLE001 — arm without the line rather than strand the gate
        logger.exception("[shop-air] eject line read failed — arming with shop air unknown")
        return EjectLine(shop=ShopAir.unknown(), margin_c=_MARGIN_DEFAULT, line_c=None)


def _drying_now(printer_id: int) -> bool:
    """Is any AMS unit on this printer drying right now? The live-only input to :func:`qualify`.

    Drying is not in ``printer_sensor_history`` (nozzle / nozzle_2 / bed / chamber only), so
    the live writer reads it off the printer through the one per-unit predicate
    (``ams_presence.unit_drying``); the backfill cannot, and treats it as not drying.
    """
    state = printer_manager.get_status(printer_id)
    units = (getattr(state, "raw_data", None) or {}).get("ams", []) if state is not None else []
    if isinstance(units, dict):
        units = units.get("ams", [])
    unit_ids: list[int] = []
    for unit in units if isinstance(units, list) else []:
        try:
            unit_ids.append(int(unit.get("id")))
        except (AttributeError, TypeError, ValueError):
            continue
    return any(ams_presence.unit_drying(printer_id, ams_id) for ams_id in unit_ids)


async def _load_minutes(
    db: AsyncSession | AsyncConnection, printer_id: int, *, since: datetime, until: datetime
) -> list[SensorMinute]:
    """One printer's history in ``[since, until]`` as minutes. ``sensor_kind IN (…)`` is
    spelled out so the ``(printer_id, sensor_kind, recorded_at)`` index serves it as four
    range seeks instead of a scan of the printer's whole retention."""
    rows = (
        await db.execute(
            select(
                PrinterSensorHistory.recorded_at,
                PrinterSensorHistory.sensor_kind,
                PrinterSensorHistory.value,
                PrinterSensorHistory.target,
            )
            .where(
                PrinterSensorHistory.printer_id == printer_id,
                PrinterSensorHistory.sensor_kind.in_(SENSOR_KINDS),
                PrinterSensorHistory.recorded_at >= since,
                PrinterSensorHistory.recorded_at <= until,
            )
            .order_by(PrinterSensorHistory.recorded_at)
        )
    ).all()
    return minutes_from_readings(rows, chamber_sensor=True)


async def note_reading(db: AsyncSession, printer_id: int, now: datetime) -> float | None:
    """The live writer: does the minute just recorded for this printer qualify? Add its sample.

    Called by ``main.record_printer_sensor_history`` right after it commits the minute, so the
    evidence this reads is the same table the backfill reads — a restart is never blind, the
    last 90 minutes are already on disk. Adds the row to ``db`` and leaves the commit to the
    caller. Returns the sample's value, or None when nothing was written.
    """
    if not supports_chamber_temp(printer_manager.get_model(printer_id)):
        return None
    minutes = await _load_minutes(db, printer_id, since=now - REST_AFTER_HEAT - MAX_EVIDENCE_GAP, until=now)
    if not minutes:
        return None
    latest = (
        await db.execute(select(func.max(ShopAirSample.recorded_at)).where(ShopAirSample.printer_id == printer_id))
    ).scalar()
    if latest is not None and minutes[-1].at - latest < SAMPLE_EVERY:
        return None
    value = qualify(minutes, drying=_drying_now(printer_id))
    if value is None:
        return None
    db.add(ShopAirSample(printer_id=printer_id, recorded_at=minutes[-1].at, value_c=value, rule_version=RULE_VERSION))
    logger.debug("[shop-air] printer %s at rest: %.1f °C at %s", printer_id, value, minutes[-1].at)
    return value


async def backfill(conn: AsyncConnection, now: datetime) -> int:
    """Rebuild the whole cache from the last :data:`HISTORY` of sensor history. Returns the count.

    The bootstrap path (``core.database.run_migrations``, keyed by :data:`RULE_VERSION`): every
    existing sample is deleted first, because a cache derived under another rule must never be
    mixed with this one, and each printer's week is walked through :func:`derive_samples` —
    the same :func:`qualify` the live writer calls. AMS drying is not recorded anywhere
    historical, so the backfill treats every minute as not drying; the cost is at most a
    drying cycle's warm-enclosure samples in the first week, which the fresh basis's
    day-curve outlier rule is built to reject.
    """
    await conn.execute(delete(ShopAirSample))
    printers = (await conn.execute(select(Printer.id, Printer.model))).all()
    since = now - HISTORY
    rows: list[dict] = []
    for printer_id, model in printers:
        if not supports_chamber_temp(model):
            continue
        minutes = await _load_minutes(conn, printer_id, since=since - REST_AFTER_HEAT - MAX_EVIDENCE_GAP, until=now)
        rows.extend(
            {"printer_id": printer_id, "recorded_at": at, "value_c": value, "rule_version": RULE_VERSION}
            for at, value in derive_samples(minutes, since=since)
        )
    if rows:
        await conn.execute(insert(ShopAirSample), rows)
    return len(rows)


async def prune(db: AsyncSession, *, before: datetime, printer_id: int | None = None) -> int:
    """Drop samples older than ``before`` — called wherever the sensor history is pruned.

    The cache never outlives its evidence: the same cutoff, the same printer scope.
    """
    statement = delete(ShopAirSample).where(ShopAirSample.recorded_at < before)
    if printer_id is not None:
        statement = statement.where(ShopAirSample.printer_id == printer_id)
    result = await db.execute(statement)
    return result.rowcount or 0
