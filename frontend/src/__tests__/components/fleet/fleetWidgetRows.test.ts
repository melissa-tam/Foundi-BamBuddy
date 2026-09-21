/**
 * The Fleet grid widgets' row builders.
 *
 * recharts draws nothing measurable in jsdom, so the thing worth testing is not
 * the picture but the NUMBERS the picture and its data table are both fed from.
 * Every assertion here is either an identity the backend states (the cause
 * bands sum to the total down, the SKU bands sum to the units) or the one rule
 * that cannot be seen by looking at a chart: an absence the recorder never
 * covered must arrive as `null`, so recharts draws a gap, and never as 0.
 *
 * The fixtures are built by aggregation from the same identities, so a builder
 * that quietly double-counted or dropped a term fails here rather than shipping.
 */
import { describe, expect, it } from 'vitest';
import {
  FIXTURE_UTC_OFFSET_MINUTES,
  HEALTHY_PRINTER_ID,
  PRODUCTION_WINDOW_DAYS,
  makeFleetOverview,
  makeFleetOverviewFirstRun,
  makeFleetOverviewProduction,
} from '../../fixtures/fleetMetrics';
import type { SeriesEnvelope, UnitsValues } from '../../../types/fleetMetrics';
import { CHART_STACK_GROUPS, DOWN_CAUSE_ORDER } from '../../../utils/fleetMetrics';
import {
  SECONDS_PER_HOUR,
  SKU_OTHER_KEY,
  cycleNotCompleted,
  cycleRows,
  downtimeCauses,
  downtimeRows,
  downtimeTotals,
  partsRows,
  partsTotals,
  printsRows,
  printsTotals,
  recoveryHeadline,
  recoveryKinds,
  recoveryRows,
  recoveryTotals,
  skuSeries,
  stateHasData,
  stateRows,
  stateTotals,
  type RowOptions,
} from '../../../components/fleet/widgets/rows';

const TODAY = 'Today';

const options = (bucket: 'hour' | 'day' | 'week' = 'day'): RowOptions => ({
  bucket,
  locale: 'en',
  todayLabel: TODAY,
});

/**
 * The day window: buckets 0 and 1 were never observed (the incident ledger is
 * all there is), bucket 3 was half observed, and bucket 6 is today.
 */
const overview = makeFleetOverview({ bucket: 'day' });
const UNOBSERVED_BUCKETS = [0, 1];
const PARTIAL_BUCKET = 3;
const CURRENT_BUCKET = 6;

describe('bucket labels', () => {
  const rows = stateRows(overview.fleet_series, options());

  it('names the current bucket rather than dating it', () => {
    expect(rows[CURRENT_BUCKET].axisLabel).toBe(TODAY);
    expect(rows[CURRENT_BUCKET].bucketLabel.isCurrent).toBe(true);
  });

  it('shows the month only where it changes, so an axis is not all "Sep"', () => {
    expect(rows[0].axisLabel).toMatch(/^\D+ \d+$/);
    expect(rows[1].axisLabel).toMatch(/^\d+$/);
  });

  it('labels in the SITE zone, not the browser one', () => {
    // The fixtures sit at UTC+12: read with UTC getters a day bucket would be
    // the day before. The full label must name the site's own date.
    expect(FIXTURE_UTC_OFFSET_MINUTES).toBe(720);
    expect(rows[0].fullLabel).toContain(rows[0].bucketLabel.dayOfMonth);
  });
});

describe('printers by state over time', () => {
  const rows = stateRows(overview.fleet_series, options());

  it('builds one row per bucket', () => {
    expect(rows).toHaveLength(overview.fleet_series.buckets.length);
  });

  it('stacks the five groups in the shared bottom-to-top order', () => {
    // The chart reads the order from this constant; the row must carry a key
    // for every one of them or a band would be silently missing.
    for (const group of CHART_STACK_GROUPS) {
      expect(Object.keys(rows[2])).toContain(group);
    }
  });

  it('reads an unobserved bucket as NO DATA, never as zero', () => {
    for (const index of UNOBSERVED_BUCKETS) {
      expect(overview.fleet_series.buckets[index].basis).toBe('incidents_only');
      expect(rows[index].printing).toBeNull();
      expect(rows[index].idle).toBeNull();
      expect(rows[index].cycle_overhead).toBeNull();
    }
  });

  it('still draws a fault the incident ledger proves in an unobserved bucket', () => {
    // Durable evidence does not depend on the recorder having been running.
    expect(rows[UNOBSERVED_BUCKETS[0]].down).toBeGreaterThan(0);
    expect(rows[UNOBSERVED_BUCKETS[0]].unobserved).toBeGreaterThan(0);
  });

  it('reads an absent class in an OBSERVED bucket as a real zero', () => {
    // `planned` only happens in bucket 2 of this fixture; bucket 4 is fully
    // observed, so its absence there is a fact and not a gap.
    expect(rows[2].planned).toBeGreaterThan(0);
    expect(rows[4].planned).toBe(0);
  });

  it('carries the roster ceiling and the peak on an observed bucket', () => {
    expect(rows[2].in_fleet).toBe(overview.fleet_series.buckets[2].values.printers_in_fleet);
    expect(rows[2].peak_down).toBe(overview.fleet_series.buckets[2].values.peak_down);
  });

  it('breaks the roster line where nobody knows the roster, rather than drawing zero', () => {
    // `printers_in_fleet` is a non-nullable float whose identity has two
    // recorder-derived terms, so on an `incidents_only` bucket it arrives as
    // `0.0` meaning UNKNOWN. A ceiling drawn along zero for the first forty
    // days of a window is the chart's loudest channel saying the farm had no
    // printers at all.
    for (const index of UNOBSERVED_BUCKETS) {
      expect(overview.fleet_series.buckets[index].values.printers_in_fleet).toBe(0);
      expect(rows[index].in_fleet).toBeNull();
    }
    expect(rows[2].in_fleet).toBeGreaterThan(0);
  });

  it('still draws the no-data band where the roster is unknown', () => {
    // Otherwise an unrecorded bucket is a red sliver floating over empty
    // space, and a fleet of one cannot be told from a fleet of twelve with
    // eleven unaccounted for.
    for (const index of UNOBSERVED_BUCKETS) {
      expect(rows[index].unobserved).toBeGreaterThan(0);
    }
  });

  it('marks the partly observed bucket for the hatch', () => {
    expect(rows[PARTIAL_BUCKET].bucketPartlyObserved).toBe(true);
    expect(rows[2].bucketPartlyObserved).toBe(false);
  });

  it('keeps the two partialities apart on every row', () => {
    // A bucket the recorder only half covered is NOT the bucket that is still
    // running, and the fixture has one of each: bucket 3 is half observed and
    // long finished, the last bucket is fully observed and still going. One
    // overloaded boolean answered the same for both, which is how a complete
    // print count ends up hatched and a half-finished day ends up trusted.
    expect(rows[PARTIAL_BUCKET].bucketInProgress).toBe(false);

    const last = rows[rows.length - 1];
    expect(last.bucketInProgress).toBe(true);
    expect(last.bucketPartlyObserved).toBe(false);
  });

  it('calls a bucket the recorder never touched partly observed, not merely unfinished', () => {
    // `incidents_only` is the backend's word for covering NONE of it, and it
    // has to read as a recorder gap even though the bucket itself is complete.
    const unobserved = rows[UNOBSERVED_BUCKETS[0]];
    expect(unobserved.bucketPartlyObserved).toBe(true);
    expect(unobserved.bucketInProgress).toBe(false);
  });

  it('summarises the window as averages, not sums', () => {
    const totals = stateTotals(overview.fleet_series);
    expect(totals.printing).toBe(overview.fleet_series.totals.avg_by_group.printing);
    expect(totals.in_fleet).toBe(overview.fleet_series.totals.printers_in_fleet);
  });

  it('reads the PRODUCTION shape without fabricating a single figure', () => {
    // An hour-old recorder under a six-week window: forty-three ledger-only
    // days, then a current day the recorder caught the last hour of.
    const production = makeFleetOverviewProduction();
    const productionRows = stateRows(production.fleet_series, options());
    const last = productionRows.length - 1;

    expect(productionRows).toHaveLength(PRODUCTION_WINDOW_DAYS);
    // The ledger still draws, so the chart is not empty…
    expect(stateHasData(productionRows)).toBe(true);
    // …but no unrecorded day asserts a roster, a printing level or an idle one.
    for (const row of productionRows.slice(0, last)) {
      expect(row.in_fleet).toBeNull();
      expect(row.printing).toBeNull();
      expect(row.idle).toBeNull();
      expect(row.cycle_overhead).toBeNull();
      expect(row.unobserved).toBeGreaterThan(0);
    }
    // The one recorded day states all of them.
    expect(productionRows[last].in_fleet).toBeGreaterThan(0);
    expect(productionRows[last].printing).not.toBeNull();
    expect(productionRows[last].bucketPartlyObserved).toBe(true);

    for (const row of productionRows) {
      for (const value of [row.printing, row.idle, row.down, row.in_fleet, row.unobserved]) {
        if (value !== null) expect(Number.isFinite(value)).toBe(true);
      }
    }
  });

  it('reads an observed bucket over the part that was RECORDED', () => {
    // The last production day was watched for its final hour, and its group
    // averages still sum to the printers KNOWN — a stack squashed to a
    // sixteenth of the roster would say the farm nearly stopped.
    const production = makeFleetOverviewProduction();
    const bucket = production.fleet_series.buckets[PRODUCTION_WINDOW_DAYS - 1]!;

    expect(bucket.observed_seconds).toBeGreaterThan(0);
    expect(bucket.observed_seconds).toBeLessThan(bucket.elapsed_seconds);
    const summed = Object.values(bucket.values.avg_by_group).reduce<number>(
      (total, value) => total + (value ?? 0),
      0,
    );
    expect(summed).toBeCloseTo(bucket.values.printers_known, 3);
  });

  it('has nothing to draw before the recorder ever ran', () => {
    const firstRun = makeFleetOverviewFirstRun();
    const firstRunRows = stateRows(firstRun.fleet_series, options());

    expect(firstRunRows.length).toBeGreaterThan(0);
    expect(stateHasData(firstRunRows)).toBe(false);
    // A peak of zero nobody watched is not a peak of zero.
    expect(firstRunRows[0].peak_down).toBeNull();
  });

  it('has data whenever any bucket was observed', () => {
    expect(stateHasData(stateRows(overview.fleet_series, options()))).toBe(true);
  });
});

describe('prints per day', () => {
  const rows = printsRows(overview.throughput, options());

  it('reads a missing outcome as zero — the print log is complete for itself', () => {
    const bucket = overview.throughput.buckets[0].values;
    expect(bucket.by_outcome.other).toBeUndefined();
    expect(rows[0].other).toBe(0);
  });

  it('keeps the bucket total the backend stated rather than re-summing it', () => {
    expect(rows[2].total).toBe(overview.throughput.buckets[2].values.total);
  });

  it('passes the per-printer rate through, nulls included', () => {
    expect(rows[2].per_printer).toBe(
      overview.throughput.buckets[2].values.prints_per_printer_per_day,
    );
  });

  it('totals the window from the envelope, not from the rows', () => {
    expect(printsTotals(overview.throughput).total).toBe(overview.throughput.totals.total);
  });
});

describe('downtime by cause', () => {
  const causes = downtimeCauses(overview.fleet_series);
  const rows = downtimeRows(overview.fleet_series, causes, options());

  /**
   * How far apart the two halves of the identity may legitimately land.
   *
   * The envelope states `avg_down` rounded to four decimal places while the
   * per-cause map is exact, so Σ causes and the total agree only to that
   * precision — and the gap GROWS with the hours the average is multiplied by.
   * The bound is therefore derived from the stated precision (half a unit in
   * the last place) rather than picked until the test passed.
   */
  const AVG_HALF_ULP = 5e-5;
  const toleranceHours = (elapsedSeconds: number): number =>
    AVG_HALF_ULP * (elapsedSeconds / SECONDS_PER_HOUR);

  it('lists only the causes the window actually had, actionable first', () => {
    expect(causes.length).toBeGreaterThan(0);
    const positions = causes.map((cause) => DOWN_CAUSE_ORDER.indexOf(cause));
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    for (const cause of causes) {
      expect(DOWN_CAUSE_ORDER).toContain(cause);
    }
  });

  it('converts average printers down into hours by the bucket ELAPSED time', () => {
    const bucket = overview.fleet_series.buckets[2];
    const expected = (bucket.values.avg_down * bucket.elapsed_seconds) / SECONDS_PER_HOUR;
    expect(rows[2].total_hours).toBeCloseTo(expected, 6);
  });

  it('divides today by elapsed, not by the bucket width', () => {
    const current = overview.fleet_series.buckets[CURRENT_BUCKET];
    expect(current.elapsed_seconds).toBeLessThan(current.seconds);
    const expected = (current.values.avg_down * current.elapsed_seconds) / SECONDS_PER_HOUR;
    expect(rows[CURRENT_BUCKET].total_hours).toBeCloseTo(expected, 6);
  });

  it('drops no cause the window contained', () => {
    // The exact half of the identity: every cause with seconds against it in
    // ANY bucket has a band, so the stack below can never be short a term.
    for (const bucket of overview.fleet_series.buckets) {
      for (const [cause, value] of Object.entries(bucket.values.avg_down_by_cause)) {
        if ((value ?? 0) > 0) expect(causes).toContain(cause);
      }
    }
  });

  it('sums the cause bands to the bucket total', () => {
    for (const row of rows) {
      const stacked = causes.reduce((total, cause) => total + (row[cause] ?? 0), 0);
      expect(Math.abs(stacked - row.total_hours)).toBeLessThanOrEqual(
        toleranceHours(row.bucketElapsedSeconds),
      );
    }
  });

  it('sums the window totals the same way', () => {
    const totals = downtimeTotals(overview.fleet_series, causes);
    const stacked = causes.reduce((total, cause) => total + (totals[cause] ?? 0), 0);
    const elapsed = overview.fleet_series.buckets.reduce(
      (total, bucket) => total + bucket.elapsed_seconds,
      0,
    );
    expect(Math.abs(stacked - totals.total_hours)).toBeLessThanOrEqual(toleranceHours(elapsed));
  });

  it('has no causes at all before the recorder ever ran', () => {
    expect(downtimeCauses(makeFleetOverviewFirstRun().fleet_series)).toEqual([]);
  });
});

describe('cooling and eject', () => {
  it('passes every statistic through unaggregated', () => {
    const rows = cycleRows(overview.cycle, { perPrinter: false });
    expect(rows).toHaveLength(overview.cycle.by_model.length);

    for (const row of rows) {
      const source = overview.cycle.by_model.find(
        (group) =>
          group.kind === row.kind && group.model === row.model && group.variant === row.variant,
      );
      expect(source).toBeDefined();
      expect(row.medianS).toBe(source?.stats.median_s);
      expect(row.p90S).toBe(source?.stats.p90_s);
      expect(row.count).toBe(source?.stats.count);
      expect(row.notCompleted).toBe(source?.stats.not_completed);
      expect(row.overExpectedShare).toBe(source?.stats.over_expected_share);
    }
  });

  it('orders cooling before ejecting, so the rows read in machine order', () => {
    const rows = cycleRows(overview.cycle, { perPrinter: false });
    expect(rows.map((row) => row.kind)).toEqual(['cooldown', 'cooldown', 'eject']);
  });

  it('leaves a cooldown with no over-expected share — it has nothing to be late against', () => {
    const cooldown = cycleRows(overview.cycle, { perPrinter: false }).find(
      (row) => row.kind === 'cooldown',
    );
    expect(cooldown?.overExpectedShare).toBeNull();
  });

  it('counts the episodes that never finished without folding them in', () => {
    const rows = cycleRows(overview.cycle, { perPrinter: false });
    const expected = overview.cycle.by_model.reduce(
      (total, group) => total + group.stats.not_completed,
      0,
    );
    expect(cycleNotCompleted(rows)).toBe(expected);
  });

  it('switches to the per-printer groups, carrying the printer id', () => {
    const rows = cycleRows(overview.cycle, { perPrinter: true });
    expect(rows).toHaveLength(overview.cycle.by_printer.length);
    expect(rows[0].printerId).toBe(HEALTHY_PRINTER_ID);
  });

  it('carries no printer id in the by-model view', () => {
    expect(cycleRows(overview.cycle, { perPrinter: false })[0].printerId).toBeNull();
  });
});

describe('recovery', () => {
  const kinds = recoveryKinds(overview.recovery.series);
  const rows = recoveryRows(overview.recovery.series, kinds, options());

  it('lists only the kinds that arrived', () => {
    expect(kinds.length).toBeGreaterThan(0);
    for (const kind of kinds) {
      expect(overview.recovery.series.totals.opened_by_kind[kind]).toBeGreaterThan(0);
    }
  });

  it('sums the kind bands to the bucket opened count', () => {
    for (const row of rows) {
      const stacked = kinds.reduce((total, kind) => total + (row[kind] ?? 0), 0);
      expect(stacked).toBe(row.opened);
    }
  });

  it('totals the window from the envelope', () => {
    const totals = recoveryTotals(overview.recovery.series, kinds);
    expect(totals.opened).toBe(overview.recovery.series.totals.opened);
  });

  it('reads fault hours open off the incident ledger, never off hours down', () => {
    const headline = recoveryHeadline(overview.recovery);
    expect(headline.faultOpenHours).toBeCloseTo(
      overview.recovery.fault_open_seconds / SECONDS_PER_HOUR,
      6,
    );
  });

  it('reads the zero-person share from the incident summary', () => {
    const headline = recoveryHeadline(overview.recovery);
    expect(headline.clearedNoPersonShare).toBeCloseTo(
      overview.recovery.summary.zero_human / overview.recovery.summary.total,
      6,
    );
  });

  it('names the kind its headline median belongs to, never blending medians', () => {
    const headline = recoveryHeadline(overview.recovery);
    const source = overview.recovery.time_to_recover.find(
      (entry) => entry.kind === headline.leadRecovery?.kind,
    );
    expect(headline.leadRecovery?.median_recover_s).toBe(source?.median_recover_s);
  });

  it('has no median at all when nothing was recovered', () => {
    const headline = recoveryHeadline({
      ...overview.recovery,
      time_to_recover: [],
      summary: { ...overview.recovery.summary, total: 0, zero_human: 0 },
    });
    expect(headline.leadRecovery).toBeNull();
    expect(headline.clearedNoPersonShare).toBeNull();
  });
});

describe('parts by SKU', () => {
  it('gives every SKU its own band while they fit, with no "other"', () => {
    const series = skuSeries(overview.units);
    expect(series.map((entry) => entry.sku)).toEqual(
      Object.keys(overview.units.totals.by_sku).sort(
        (left, right) =>
          overview.units.totals.by_sku[right] - overview.units.totals.by_sku[left] ||
          (left < right ? -1 : 1),
      ),
    );
    expect(series.some((entry) => entry.sku === null)).toBe(false);
  });

  it('sums the bands to the bucket units in every bucket', () => {
    const series = skuSeries(overview.units);
    const rows = partsRows(overview.units, series, options());
    for (const row of rows) {
      const stacked = series.reduce((total, entry) => total + (row[entry.key] ?? 0), 0);
      expect(stacked).toBe(row.units);
    }
  });

  it('sums the window totals to the window units', () => {
    const series = skuSeries(overview.units);
    const totals = partsTotals(overview.units, series);
    const stacked = series.reduce((total, entry) => total + (totals[entry.key] ?? 0), 0);
    expect(stacked).toBe(overview.units.totals.units);
  });

  describe('with more SKUs than the legend can carry', () => {
    /** Seven SKUs, descending, so the top five are unambiguous. */
    const wide: SeriesEnvelope<UnitsValues> = {
      buckets: overview.units.buckets.map((bucket, index) => ({
        ...bucket,
        values: {
          units: 70 + index,
          plates: 35,
          by_sku: {
            'A.1': 20,
            'B.2': 15,
            'C.3': 12,
            'D.4': 10,
            'E.5': 6,
            'F.6': 4,
            'G.7': 3 + index,
          },
        },
      })),
      totals: {
        units: 490 + 21,
        plates: 245,
        by_sku: {
          'A.1': 140,
          'B.2': 105,
          'C.3': 84,
          'D.4': 70,
          'E.5': 42,
          'F.6': 28,
          'G.7': 21 + 21,
        },
      },
    };

    it('keeps the top five by WINDOW total and folds the rest into "other"', () => {
      const series = skuSeries(wide);
      expect(series.map((entry) => entry.sku)).toEqual([
        'A.1',
        'B.2',
        'C.3',
        'D.4',
        'E.5',
        null,
      ]);
      expect(series[series.length - 1].key).toBe(SKU_OTHER_KEY);
    });

    it('keys a SKU positionally, because a dotted dataKey is a recharts PATH', () => {
      // `get(row, 'A.1')` would look for row.A["1"] and find nothing.
      for (const entry of skuSeries(wide)) {
        expect(entry.key).not.toContain('.');
      }
    });

    it('makes "other" the remainder of the bucket total, so the bands still add up', () => {
      const series = skuSeries(wide);
      const rows = partsRows(wide, series, options());
      for (const row of rows) {
        const stacked = series.reduce((total, entry) => total + (row[entry.key] ?? 0), 0);
        expect(stacked).toBe(row.units);
        expect(row[SKU_OTHER_KEY]).toBeGreaterThan(0);
      }
    });

    it('adds the window totals up the same way', () => {
      const series = skuSeries(wide);
      const totals = partsTotals(wide, series);
      const stacked = series.reduce((total, entry) => total + (totals[entry.key] ?? 0), 0);
      expect(stacked).toBe(wide.totals.units);
    });
  });
});
