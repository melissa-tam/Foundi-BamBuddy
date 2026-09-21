/**
 * Fleet-metrics test fixtures.
 *
 * Built by AGGREGATION, never by hand-typed totals: a bucket's fleet cell is
 * summed from its printer cells, a series' totals are summed from its buckets,
 * and each printer-bucket's class seconds are filled to exactly the bucket's
 * `elapsed_seconds`. That is the backend's own set of identities
 * (`services/fleet_metrics` §Identities), and a fixture that violated one would
 * let a component ship a bug the real API would expose — the classic way a
 * green suite certifies an interface nobody has.
 *
 * The site zone is deliberately NOT UTC (`utc_offset_minutes: 720`), so a day
 * bucket starts at 12:00 UTC the day before. A component that formats a bucket
 * with the browser's timezone renders the wrong date against these fixtures,
 * which is the point.
 */

import type {
  CycleProjection,
  FleetBucket,
  FleetClassKey,
  FleetOverview,
  FleetSeriesValues,
  FleetStatus,
  MatrixCell,
  MatrixPrinter,
  MatrixValues,
  PrintOutcome,
  PrinterIntervalsResponse,
  PrinterStatus,
  RecoveryProjection,
  RecoveryValues,
  SeriesBucket,
  SeriesEnvelope,
  SummaryRow,
  SummaryRowKey,
  ThroughputValues,
  UnitsValues,
} from '../../types/fleetMetrics';

/** The fixtures' site zone: UTC+12, so nothing accidentally passes in UTC. */
export const FIXTURE_UTC_OFFSET_MINUTES = 720;
export const FIXTURE_TZ_NAME = 'Pacific/Auckland';
/** The site's today. Every fixture window ends here. */
export const FIXTURE_SITE_TODAY = '2026-09-21';

const HOUR_S = 3600;
const DAY_S = 86_400;
const WEEK_S = 604_800;

const BUCKET_SECONDS: Record<FleetBucket, number> = { hour: HOUR_S, day: DAY_S, week: WEEK_S };

/** The roster every fixture window is about. */
export const FIXTURE_PRINTERS: MatrixPrinter[] = [
  {
    printer_id: 1,
    name: '001-H2S',
    model: 'H2S',
    is_active: true,
    deleted: false,
    hours_down_per_day: 0.2,
    prints_per_day: 11.4,
  },
  {
    printer_id: 3,
    name: '009-H2C',
    model: 'H2C',
    is_active: true,
    deleted: false,
    hours_down_per_day: 4.4,
    prints_per_day: 3.1,
  },
  {
    printer_id: 4,
    name: '011-H2S',
    model: 'H2S',
    is_active: false,
    deleted: false,
    hours_down_per_day: null,
    prints_per_day: null,
  },
  {
    printer_id: 9,
    name: 'Printer 9',
    model: null,
    is_active: false,
    deleted: true,
    hours_down_per_day: null,
    prints_per_day: null,
  },
];

/** The healthy workhorse, the problem printer, the deactivated one, the deleted one. */
export const HEALTHY_PRINTER_ID = 1;
export const PROBLEM_PRINTER_ID = 3;
export const DEACTIVATED_PRINTER_ID = 4;
export const DELETED_PRINTER_ID = 9;

// ── low-level builders ──────────────────────────────────────────────────────

type ClassSeconds = Partial<Record<FleetClassKey, number>>;

const sum = (map: Partial<Record<string, number>>): number =>
  Object.values(map).reduce<number>((total, value) => total + (value ?? 0), 0);

const addInto = (target: Record<string, number>, source: Partial<Record<string, number>>): void => {
  for (const [key, value] of Object.entries(source)) {
    if (value === undefined) continue;
    target[key] = (target[key] ?? 0) + value;
  }
};

const downSeconds = (classes: ClassSeconds): number =>
  Object.entries(classes).reduce<number>(
    (total, [key, value]) => (key.startsWith('down') ? total + (value ?? 0) : total),
    0,
  );

/**
 * One printer's cell, with the remainder of the bucket filled into `filler`.
 *
 * The fill is what keeps the identity exact: a caller states the interesting
 * seconds and the cell always sums to `elapsed`.
 */
export function makeCell(
  elapsed: number,
  classes: ClassSeconds,
  options: { filler?: FleetClassKey; prints?: Partial<Record<PrintOutcome, number>>; basis?: 'observed' | 'incidents_only' } = {},
): MatrixCell {
  const { filler = 'idle', prints = {}, basis = 'observed' } = options;
  const stated = sum(classes);
  const remainder = Math.max(0, elapsed - stated);
  const full: ClassSeconds = { ...classes };
  if (remainder > 0) full[filler] = (full[filler] ?? 0) + remainder;
  return { class_seconds: full, down_seconds: downSeconds(full), prints, basis };
}

/** Sum printer cells into the fleet row. */
function foldFleetCell(cells: MatrixCell[]): MatrixCell {
  const classes: Record<string, number> = {};
  const prints: Record<string, number> = {};
  let down = 0;
  let incidentsOnly = 0;
  for (const cell of cells) {
    addInto(classes, cell.class_seconds);
    addInto(prints, cell.prints);
    down += cell.down_seconds;
    if (cell.basis === 'incidents_only') incidentsOnly += 1;
  }
  return {
    class_seconds: classes as ClassSeconds,
    down_seconds: down,
    prints: prints as Partial<Record<PrintOutcome, number>>,
    basis: incidentsOnly === cells.length ? 'incidents_only' : 'observed',
  };
}

/** Bucket starts, in naive UTC, for a site-local grid of `count` buckets ending "now". */
function bucketStarts(bucket: FleetBucket, count: number, lastSiteStart: string): string[] {
  const width = BUCKET_SECONDS[bucket] * 1000;
  // The site-local wall clock of the last bucket, shifted back to naive UTC.
  const lastUtcMs = Date.parse(`${lastSiteStart}Z`) - FIXTURE_UTC_OFFSET_MINUTES * 60_000;
  return Array.from({ length: count }, (_, index) =>
    new Date(lastUtcMs - (count - 1 - index) * width).toISOString().slice(0, 19),
  );
}

// ── the window spec ─────────────────────────────────────────────────────────

interface BucketShape {
  start: string;
  seconds: number;
  elapsed_seconds: number;
  observed_seconds: number;
}

interface WindowSpec {
  bucket: FleetBucket;
  shapes: BucketShape[];
  /** Per bucket index, per printer id, the cell. */
  cell: (bucketIndex: number, printer: MatrixPrinter, shape: BucketShape) => MatrixCell;
}

function buildMatrix(spec: WindowSpec): {
  series: SeriesEnvelope<MatrixValues>;
  perBucketCells: Record<string, MatrixCell>[];
} {
  const perBucketCells: Record<string, MatrixCell>[] = [];
  const buckets: SeriesBucket<MatrixValues>[] = spec.shapes.map((shape, index) => {
    const printers: Record<string, MatrixCell> = {};
    for (const printer of FIXTURE_PRINTERS) {
      printers[String(printer.printer_id)] = spec.cell(index, printer, shape);
    }
    perBucketCells.push(printers);
    return {
      start: shape.start,
      seconds: shape.seconds,
      elapsed_seconds: shape.elapsed_seconds,
      observed_seconds: shape.observed_seconds,
      utc_offset_minutes: FIXTURE_UTC_OFFSET_MINUTES,
      basis: shape.observed_seconds > 0 ? 'observed' : 'incidents_only',
      values: { printers, fleet: foldFleetCell(Object.values(printers)) },
    };
  });

  // Totals: the same shape re-aggregated over the window, summed not averaged.
  const totalPrinters: Record<string, MatrixCell> = {};
  for (const printer of FIXTURE_PRINTERS) {
    const key = String(printer.printer_id);
    const classes: Record<string, number> = {};
    const prints: Record<string, number> = {};
    let down = 0;
    let anyObserved = false;
    for (const cells of perBucketCells) {
      const cell = cells[key];
      if (!cell) continue;
      addInto(classes, cell.class_seconds);
      addInto(prints, cell.prints);
      down += cell.down_seconds;
      if (cell.basis === 'observed') anyObserved = true;
    }
    totalPrinters[key] = {
      class_seconds: classes as ClassSeconds,
      down_seconds: down,
      prints: prints as Partial<Record<PrintOutcome, number>>,
      basis: anyObserved ? 'observed' : 'incidents_only',
    };
  }

  return {
    series: {
      buckets,
      totals: { printers: totalPrinters, fleet: foldFleetCell(Object.values(totalPrinters)) },
    },
    perBucketCells,
  };
}

/**
 * Average concurrent printers per group, straight off the matrix cells.
 *
 * Two shapes, because the backend has two. The divisor is the RECORDED part of
 * the bucket, not its elapsed span: a bucket the recorder caught the last 45
 * minutes of still reads `printing 8 · cooling 1 · down 3` and those still sum
 * to the printers KNOWN, so the chart draws a full stack and the partly
 * observed marker — not a stack squashed to a sixteenth of the roster. The
 * `unobserved` group is therefore absent from an observed bucket: there is no
 * blind spot inside the part that was watched.
 *
 * On an `incidents_only` bucket nothing was measured at all, so the response
 * states only what the fault and hold LEDGER proves — `down` and `planned` —
 * and `printers_in_fleet` is `0.0` for UNKNOWN, because its identity
 * (`printers_known − out_of_fleet − not_recorded`) has two recorder-derived
 * terms and the field is a non-nullable float.
 */
function fleetValuesFrom(
  cells: Record<string, MatrixCell>,
  elapsed: number,
  observedSeconds: number,
): FleetSeriesValues {
  const observed = observedSeconds > 0;
  const byGroup: Record<string, number> = {};
  const byCause: Record<string, number> = {};
  let down = 0;
  let printing = 0;
  for (const cell of Object.values(cells)) {
    // Each printer contributes exactly 1.0 of ITSELF, spread over the part of
    // the bucket that was recorded for it — which is the elapsed span less its
    // own `unobserved` seconds. That per-cell denominator is what makes the
    // groups sum to the printers KNOWN however short the recorder fell; a
    // single bucket-wide divisor cannot, because a deactivated printer's
    // out-of-fleet stretch is stated over the whole span while a running one's
    // states are stated over the watched part.
    // A bucket nobody watched has no per-cell recorded part, so its ledger
    // hours divide by the whole span — the ledger covered all of it.
    const gap = observed ? (cell.class_seconds.unobserved ?? 0) : 0;
    const divisor = Math.max(1, elapsed - gap);
    for (const [key, seconds] of Object.entries(cell.class_seconds)) {
      if (!seconds) continue;
      const separator = key.indexOf(':');
      const group = separator === -1 ? key : key.slice(0, separator);
      // The recorder's gap is not a state it measured, so it is not a band in
      // an observed bucket's reading of itself.
      if (group === 'unobserved') continue;
      byGroup[group] = (byGroup[group] ?? 0) + seconds / divisor;
      if (group === 'printing') printing += seconds / divisor;
      if (group === 'down') {
        const cause = key.slice(separator + 1);
        byCause[cause] = (byCause[cause] ?? 0) + seconds / divisor;
        down += seconds / divisor;
      }
    }
  }
  const known = FIXTURE_PRINTERS.length;

  if (!observed) {
    const ledger: Record<string, number> = {};
    if (byGroup.down !== undefined) ledger.down = round(byGroup.down);
    if (byGroup.planned !== undefined) ledger.planned = round(byGroup.planned);
    return {
      printers_known: known,
      // The wire's spelling of "unknown" on a non-nullable float.
      printers_in_fleet: 0,
      avg_by_group: ledger as FleetSeriesValues['avg_by_group'],
      avg_down_by_cause: byCause as FleetSeriesValues['avg_down_by_cause'],
      avg_down: round(down),
      peak_down: Math.ceil(down),
      uptime: null,
      time_printing: null,
    };
  }

  const inFleet = known - (byGroup.out_of_fleet ?? 0) - (byGroup.not_recorded ?? 0);
  const counted = inFleet - (byGroup.planned ?? 0);
  return {
    printers_known: known,
    printers_in_fleet: round(inFleet),
    avg_by_group: byGroup as FleetSeriesValues['avg_by_group'],
    avg_down_by_cause: byCause as FleetSeriesValues['avg_down_by_cause'],
    avg_down: round(down),
    peak_down: Math.ceil(down),
    uptime: counted > 0 ? round((counted - down) / counted) : null,
    time_printing: counted > 0 ? round(printing / counted) : null,
  };
}

const round = (value: number): number => Math.round(value * 10_000) / 10_000;

/**
 * Prints, from the print log.
 *
 * `printerDays` is passed rather than derived, because the two rates have
 * different denominators on purpose: "Prints / day" covers the WHOLE window
 * (the print log is complete for its own history), while "Prints per printer /
 * day" divides by counted printer-days, which only a RECORDED bucket produces.
 * On a young instance the two are therefore not comparable with each other,
 * which is what the row's hint now says.
 */
function throughputValuesFrom(
  cells: Record<string, MatrixCell>,
  elapsed: number,
  printerDays: number,
): ThroughputValues {
  const byOutcome: Record<string, number> = {};
  const byPrinter: Record<string, Partial<Record<PrintOutcome, number>>> = {};
  for (const [id, cell] of Object.entries(cells)) {
    if (sum(cell.prints) > 0) byPrinter[id] = { ...cell.prints };
    addInto(byOutcome, cell.prints);
  }
  const completed = byOutcome.completed ?? 0;
  const failed = byOutcome.failed ?? 0;
  const days = elapsed / DAY_S;
  return {
    by_outcome: byOutcome as Partial<Record<PrintOutcome, number>>,
    total: sum(byOutcome),
    by_printer: byPrinter,
    prints_per_day: days > 0 ? round(completed / days) : null,
    prints_per_printer_per_day: printerDays > 0 ? round(completed / printerDays) : null,
    success_pct: completed + failed > 0 ? round(completed / (completed + failed)) : null,
  };
}

const SUMMARY_KEYS: SummaryRowKey[] = [
  'avg_printing',
  'avg_cycle_overhead',
  'avg_idle',
  'avg_down',
  'avg_planned',
  'peak_down',
  'printers_in_fleet',
  'prints_per_day',
  'prints_per_printer_per_day',
  'uptime',
  'time_printing',
];

function readSummary(
  key: SummaryRowKey,
  fleet: FleetSeriesValues,
  prints: ThroughputValues,
  observed: boolean,
): number | null {
  switch (key) {
    case 'avg_printing':
      return observed ? (fleet.avg_by_group.printing ?? 0) : null;
    case 'avg_cycle_overhead':
      return observed ? (fleet.avg_by_group.cycle_overhead ?? 0) : null;
    case 'avg_idle':
      return observed ? (fleet.avg_by_group.idle ?? 0) : null;
    case 'avg_down':
      return observed ? fleet.avg_down : null;
    case 'avg_planned':
      return observed ? (fleet.avg_by_group.planned ?? 0) : null;
    case 'peak_down':
      return observed ? fleet.peak_down : null;
    case 'printers_in_fleet':
      return observed ? fleet.printers_in_fleet : null;
    case 'prints_per_day':
      return prints.prints_per_day;
    case 'prints_per_printer_per_day':
      return prints.prints_per_printer_per_day;
    case 'uptime':
      return observed ? fleet.uptime : null;
    default:
      return observed ? fleet.time_printing : null;
  }
}

// ── the cell recipe ─────────────────────────────────────────────────────────

/**
 * What each printer was doing in each bucket.
 *
 * The recipe is where the fixtures' STORY lives: a leading stretch the recorder
 * did not cover (only the incident ledger did), one partly observed bucket, a
 * workhorse, a printer with real downtime, a deactivated one and a deleted one.
 */
interface CellOptions {
  /**
   * The unrecorded stretch carries what a REAL instance's does: prints (the
   * print log is complete for its own history and does not stop because the
   * state recorder was not running yet) and a declared service hold beside the
   * faults, so `avg_by_group` there holds both ledger groups.
   *
   * Opt-in so the older fixtures keep the exact shapes their tests pin.
   */
  production?: boolean;
}

function defaultCell(
  index: number,
  printer: MatrixPrinter,
  shape: BucketShape,
  { production = false }: CellOptions = {},
): MatrixCell {
  const elapsed = shape.elapsed_seconds;
  if (elapsed <= 0) return makeCell(0, {}, { filler: 'idle', basis: 'observed' });

  if (printer.printer_id === DEACTIVATED_PRINTER_ID || printer.printer_id === DELETED_PRINTER_ID) {
    return makeCell(elapsed, {}, { filler: 'out_of_fleet' });
  }

  // Buckets the recorder never covered: the fault ledger is the only evidence.
  if (shape.observed_seconds === 0) {
    const fault = printer.printer_id === PROBLEM_PRINTER_ID ? Math.min(4 * HOUR_S, elapsed) : 0;
    const hold =
      production && printer.printer_id === HEALTHY_PRINTER_ID && index % 7 === 0
        ? Math.min(2 * HOUR_S, elapsed)
        : 0;
    return makeCell(
      elapsed,
      {
        ...(fault > 0 ? { 'down:fault:jam': fault } : {}),
        ...(hold > 0 ? { planned: hold } : {}),
      },
      {
        filler: 'unobserved',
        basis: 'incidents_only',
        prints: production
          ? { completed: 8 + (index % 5), failed: index % 4 === 0 ? 1 : 0 }
          : {},
      },
    );
  }

  const partial = shape.observed_seconds < shape.elapsed_seconds;
  const unobserved = partial ? elapsed - shape.observed_seconds : 0;
  const active = elapsed - unobserved;

  if (printer.printer_id === PROBLEM_PRINTER_ID) {
    const down = Math.min(Math.round(active * 0.35), active);
    const printing = Math.round((active - down) * 0.5);
    return makeCell(
      elapsed,
      {
        printing,
        'cycle_overhead:cooling': Math.round(printing * 0.12),
        'down:fault:jam': Math.round(down * 0.6),
        'down:plate_held': down - Math.round(down * 0.6),
        ...(unobserved > 0 ? { unobserved } : {}),
      },
      { prints: { completed: 3 + (index % 2), failed: index % 3 === 0 ? 1 : 0 } },
    );
  }

  const printing = Math.round(active * 0.62);
  return makeCell(
    elapsed,
    {
      printing,
      'cycle_overhead:cooling': Math.round(active * 0.08),
      'cycle_overhead:ejecting': Math.round(active * 0.02),
      ...(index === 2 ? { planned: Math.round(active * 0.1) } : {}),
      ...(unobserved > 0 ? { unobserved } : {}),
    },
    { prints: { completed: 10 + (index % 4), cancelled: index === 1 ? 1 : 0 } },
  );
}

// ── projections that do not carry an identity ───────────────────────────────

function makeCycle(): CycleProjection {
  return {
    by_model: [
      {
        kind: 'cooldown',
        model: 'H2S',
        variant: 'hold',
        stats: { count: 42, median_s: 512, p90_s: 690, not_completed: 1, over_expected: null, over_expected_share: null },
      },
      {
        kind: 'cooldown',
        model: 'H2S',
        variant: 'fan_only',
        stats: { count: 18, median_s: 744, p90_s: 980, not_completed: 0, over_expected: null, over_expected_share: null },
      },
      {
        kind: 'eject',
        model: 'H2S',
        variant: 'production',
        stats: { count: 60, median_s: 85, p90_s: 102, not_completed: 2, over_expected: 5, over_expected_share: 0.083 },
      },
    ],
    by_printer: [
      {
        printer_id: HEALTHY_PRINTER_ID,
        kind: 'eject',
        model: 'H2S',
        variant: 'production',
        stats: { count: 40, median_s: 83, p90_s: 96, not_completed: 1, over_expected: 2, over_expected_share: 0.05 },
      },
    ],
  };
}

function makeRecovery(shapes: BucketShape[]): RecoveryProjection {
  const buckets: SeriesBucket<RecoveryValues>[] = shapes.map((shape, index) => ({
    start: shape.start,
    seconds: shape.seconds,
    elapsed_seconds: shape.elapsed_seconds,
    observed_seconds: shape.observed_seconds,
    utc_offset_minutes: FIXTURE_UTC_OFFSET_MINUTES,
    // Incident data is complete for its own history: no basis, ever.
    basis: null,
    values: index % 3 === 0 ? { opened: 1, opened_by_kind: { jam: 1 } } : { opened: 0, opened_by_kind: {} },
  }));
  const opened = buckets.reduce((total, entry) => total + entry.values.opened, 0);
  return {
    series: {
      buckets,
      totals: { opened, opened_by_kind: { jam: opened } },
    },
    summary: {
      total: opened,
      zero_human: 1,
      declared: 1,
      by_outcome: { auto_recovered: 1, human_resolved: Math.max(0, opened - 1) },
      by_kind: { jam: { auto_recovered: 1, human_resolved: Math.max(0, opened - 1) } },
    },
    time_to_recover: [
      { kind: 'jam', count: opened, open_count: 1, total_held_s: 5 * HOUR_S, median_recover_s: 3600, p90_recover_s: 9000 },
    ],
    fault_open_seconds: 5 * HOUR_S,
  };
}

function makeUnits(shapes: BucketShape[]): SeriesEnvelope<UnitsValues> {
  const buckets: SeriesBucket<UnitsValues>[] = shapes.map((shape, index) => ({
    start: shape.start,
    seconds: shape.seconds,
    elapsed_seconds: shape.elapsed_seconds,
    observed_seconds: shape.observed_seconds,
    utc_offset_minutes: FIXTURE_UTC_OFFSET_MINUTES,
    basis: null,
    values: { units: 24 + index, plates: 12 + index, by_sku: { 'SKU007.01': 16 + index, 'SKU012.02': 8 } },
  }));
  const units = buckets.reduce((total, entry) => total + entry.values.units, 0);
  const plates = buckets.reduce((total, entry) => total + entry.values.plates, 0);
  return {
    buckets,
    totals: {
      units,
      plates,
      by_sku: {
        'SKU007.01': buckets.reduce((t, e) => t + (e.values.by_sku['SKU007.01'] ?? 0), 0),
        'SKU012.02': buckets.reduce((t, e) => t + (e.values.by_sku['SKU012.02'] ?? 0), 0),
      },
    },
  };
}

// ── the overview builder ────────────────────────────────────────────────────

export interface FleetOverviewOptions {
  bucket?: FleetBucket;
  /** How many buckets. Defaults: 7 day, 6 week, 12 hour. */
  count?: number;
  /** Buckets (from the start) the recorder did not cover at all. */
  incidentsOnlyLead?: number;
  /** Bucket index that is only half observed. */
  partialIndex?: number | null;
  /** The last bucket is the current one: elapsed short of its width. */
  currentLast?: boolean;
  /** Nothing has ever been observed: the first-run variant. */
  firstRun?: boolean;
  /**
   * Exactly how much of the LAST bucket the recorder covered, in seconds.
   *
   * The production shape's whole point: a recorder an hour old under a window
   * weeks long, which `incidentsOnlyLead` alone cannot express because it only
   * says "none of it".
   */
  lastObservedSeconds?: number;
  /** The unrecorded stretch carries prints and holds — see `CellOptions`. */
  production?: boolean;
  /**
   * Trailing buckets that have NOT HAPPENED: `elapsed_seconds: 0`.
   *
   * A window is a grid, so a window ending today carries the hours (or the
   * day) still to come. They are not an edge case — every `Today` view has
   * them from midnight until 23:00.
   */
  futureTail?: number;
  dateFrom?: string;
  dateTo?: string;
}

const DEFAULT_COUNT: Record<FleetBucket, number> = { hour: 12, day: 7, week: 6 };

export function makeFleetOverview(options: FleetOverviewOptions = {}): FleetOverview {
  const bucket = options.bucket ?? 'day';
  const count = options.count ?? DEFAULT_COUNT[bucket];
  const firstRun = options.firstRun ?? false;
  const incidentsOnlyLead = firstRun ? count : (options.incidentsOnlyLead ?? 2);
  const partialIndex = firstRun ? null : options.partialIndex === undefined ? 3 : options.partialIndex;
  const currentLast = options.currentLast ?? true;

  const width = BUCKET_SECONDS[bucket];
  const lastSiteStart =
    bucket === 'hour' ? `${FIXTURE_SITE_TODAY}T09:00:00` : `${FIXTURE_SITE_TODAY}T00:00:00`;
  const starts = bucketStarts(bucket, count, lastSiteStart);

  const production = options.production ?? false;

  const futureTail = options.futureTail ?? 0;
  /** The bucket NOW falls in: the last one before the buckets still to come. */
  const currentIndex = count - 1 - futureTail;

  const shapes: BucketShape[] = starts.map((start, index) => {
    // Past the current bucket is the future: no elapsed time, so nothing to
    // observe and nothing to be zero.
    if (index > currentIndex) {
      return { start, seconds: width, elapsed_seconds: 0, observed_seconds: 0 };
    }
    const isCurrent = index === currentIndex;
    const elapsed = isCurrent && currentLast ? Math.round(width / 2) : width;
    let observed = elapsed;
    if (index < incidentsOnlyLead) observed = 0;
    else if (index === partialIndex) observed = Math.round(elapsed / 2);
    if (isCurrent && options.lastObservedSeconds !== undefined) {
      observed = Math.min(elapsed, options.lastObservedSeconds);
    }
    return { start, seconds: width, elapsed_seconds: elapsed, observed_seconds: observed };
  });

  const { series: matrixSeries, perBucketCells } = buildMatrix({
    bucket,
    shapes,
    cell: (index, printer, shape) =>
      firstRun
        ? makeCell(shape.elapsed_seconds, {}, { filler: 'not_recorded', basis: 'incidents_only' })
        : defaultCell(index, printer, shape, { production }),
  });

  const fleetBuckets: SeriesBucket<FleetSeriesValues>[] = shapes.map((shape, index) => ({
    start: shape.start,
    seconds: shape.seconds,
    elapsed_seconds: shape.elapsed_seconds,
    observed_seconds: shape.observed_seconds,
    utc_offset_minutes: FIXTURE_UTC_OFFSET_MINUTES,
    basis: shape.observed_seconds > 0 ? 'observed' : 'incidents_only',
    values: fleetValuesFrom(
      perBucketCells[index] ?? {},
      Math.max(1, shape.elapsed_seconds),
      shape.observed_seconds,
    ),
  }));

  const totalElapsed = shapes.reduce((total, shape) => total + shape.elapsed_seconds, 0);
  const totalObserved = shapes.reduce((total, shape) => total + shape.observed_seconds, 0);
  const fleetTotals = fleetValuesFrom(
    matrixSeries.totals.printers,
    Math.max(1, totalElapsed),
    totalObserved,
  );
  fleetTotals.peak_down = Math.max(...fleetBuckets.map((entry) => entry.values.peak_down), 0);

  const throughputBuckets: SeriesBucket<ThroughputValues>[] = shapes.map((shape, index) => ({
    start: shape.start,
    seconds: shape.seconds,
    elapsed_seconds: shape.elapsed_seconds,
    observed_seconds: shape.observed_seconds,
    utc_offset_minutes: FIXTURE_UTC_OFFSET_MINUTES,
    // Print data is complete for its own history — the backend never marks it.
    basis: null,
    // Counted printer-days come from the RECORDED part of the bucket, so an
    // `incidents_only` bucket contributes none and its per-printer rate is
    // null — while its plain prints-per-day is a real figure from the log.
    values: throughputValuesFrom(
      perBucketCells[index] ?? {},
      Math.max(1, shape.elapsed_seconds),
      (shape.observed_seconds / DAY_S) * (fleetBuckets[index]?.values.printers_in_fleet ?? 0),
    ),
  }));
  const throughputTotals = throughputValuesFrom(
    matrixSeries.totals.printers,
    Math.max(1, totalElapsed),
    shapes.reduce(
      (total, shape, index) =>
        total +
        (shape.observed_seconds / DAY_S) * (fleetBuckets[index]?.values.printers_in_fleet ?? 0),
      0,
    ),
  );

  const observed = shapes.some((shape) => shape.observed_seconds > 0);
  const rows: SummaryRow[] = SUMMARY_KEYS.map((key) => ({
    key,
    figure: readSummary(key, fleetTotals, throughputTotals, observed),
    previous: observed ? readSummary(key, fleetTotals, throughputTotals, observed) : null,
    series: fleetBuckets.map((entry, index) =>
      readSummary(
        key,
        entry.values,
        throughputBuckets[index]?.values ?? throughputTotals,
        entry.basis === 'observed',
      ),
    ),
  }));

  const firstStart = shapes[0]?.start ?? `${FIXTURE_SITE_TODAY}T00:00:00`;
  const lastShape = shapes[shapes.length - 1];
  return {
    date_from: options.dateFrom ?? siteDateOf(firstStart),
    date_to: options.dateTo ?? FIXTURE_SITE_TODAY,
    bucket,
    tz_name: FIXTURE_TZ_NAME,
    generated_at: `${FIXTURE_SITE_TODAY}T00:04:00`,
    window_start: firstStart,
    window_end: lastShape
      ? new Date(Date.parse(`${lastShape.start}Z`) + lastShape.seconds * 1000).toISOString().slice(0, 19)
      : firstStart,
    summary: { rows },
    matrix: { printers: FIXTURE_PRINTERS, series: matrixSeries },
    fleet_series: { buckets: fleetBuckets, totals: fleetTotals },
    throughput: { buckets: throughputBuckets, totals: throughputTotals },
    units: makeUnits(shapes),
    cycle: makeCycle(),
    recovery: makeRecovery(shapes),
  };
}

/** The site-local calendar date of a naive-UTC instant, under the fixture offset. */
export function siteDateOf(naiveUtc: string): string {
  return new Date(Date.parse(`${naiveUtc}Z`) + FIXTURE_UTC_OFFSET_MINUTES * 60_000)
    .toISOString()
    .slice(0, 10);
}

/** The day variant — the default every MSW handler serves. */
export const makeFleetOverviewDay = (): FleetOverview => makeFleetOverview({ bucket: 'day' });
export const makeFleetOverviewWeek = (): FleetOverview => makeFleetOverview({ bucket: 'week' });
export const makeFleetOverviewHour = (): FleetOverview => makeFleetOverview({ bucket: 'hour' });
/** Nothing observed yet: every bucket is `not_recorded`, every state row null. */
export const makeFleetOverviewFirstRun = (): FleetOverview => makeFleetOverview({ firstRun: true });

/** How many of the today-shaped hour grid's twenty-four hours are still to come. */
export const TODAY_FUTURE_HOURS = 6;

/**
 * TODAY, as an hour grid: eighteen hours that have happened and six that have
 * not.
 *
 * The shape the matrix is read in every morning, and the one that exposed three
 * lies at once — future hours claiming an observed zero under a full hatch, the
 * per-row Details control opening 23:00, and the "Today" chip landing on a
 * single hour column.
 */
export const makeFleetOverviewTodayHours = (): FleetOverview =>
  makeFleetOverview({
    bucket: 'hour',
    count: 24,
    incidentsOnlyLead: 0,
    partialIndex: null,
    currentLast: true,
    futureTail: TODAY_FUTURE_HOURS,
  });

/** How many days the production-shaped window spans. */
export const PRODUCTION_WINDOW_DAYS = 44;

/**
 * PRODUCTION, as of this wave: a state recorder under an hour old beneath a
 * window six weeks deep.
 *
 * Forty-three `incidents_only` days carrying fault and hold seconds and a full
 * print log, then a current day the recorder caught the last hour of. It is not
 * an edge case — it is what every farm's Fleet tab looks like for its first
 * month — and it is the shape under which the tab has to have no NaN, no
 * Infinity, no raw i18n key and no figure presented as a reading that nobody
 * took.
 */
export const makeFleetOverviewProduction = (): FleetOverview =>
  makeFleetOverview({
    bucket: 'day',
    count: PRODUCTION_WINDOW_DAYS,
    incidentsOnlyLead: PRODUCTION_WINDOW_DAYS - 1,
    partialIndex: null,
    currentLast: true,
    lastObservedSeconds: HOUR_S,
    production: true,
  });

// ── status ──────────────────────────────────────────────────────────────────

export interface FleetStatusOptions {
  /** No observation has ever been recorded: `recording_since` is null. */
  firstRun?: boolean;
  generatedAt?: string;
  printers?: PrinterStatus[];
  /** The recorder's first instant. Overrides `firstRun`'s null. */
  recordingSince?: string | null;
  /** The earlier of that and the first incident — what "all time" resolves to. */
  historySince?: string | null;
}

const STATUS_PRINTERS: PrinterStatus[] = [
  {
    printer_id: HEALTHY_PRINTER_ID,
    name: '001-H2S',
    model: 'H2S',
    is_active: true,
    deleted: false,
    class_key: 'printing',
    group: 'printing',
    cause: null,
    since: '2026-09-20T22:10:00',
    since_open_ended: false,
  },
  {
    printer_id: PROBLEM_PRINTER_ID,
    name: '009-H2C',
    model: 'H2C',
    is_active: true,
    deleted: false,
    class_key: 'down:fault:jam',
    group: 'down',
    cause: 'fault:jam',
    since: '2026-09-20T20:02:00',
    since_open_ended: false,
  },
  {
    printer_id: DEACTIVATED_PRINTER_ID,
    name: '011-H2S',
    model: 'H2S',
    is_active: false,
    deleted: false,
    class_key: 'out_of_fleet',
    group: 'out_of_fleet',
    cause: null,
    since: null,
    since_open_ended: true,
  },
];

export function makeFleetStatus(options: FleetStatusOptions = {}): FleetStatus {
  const printers = options.printers ?? STATUS_PRINTERS;
  const countsByGroup: Record<string, number> = {};
  const countsByClass: Record<string, number> = {};
  for (const printer of printers) {
    countsByGroup[printer.group] = (countsByGroup[printer.group] ?? 0) + 1;
    countsByClass[printer.class_key] = (countsByClass[printer.class_key] ?? 0) + 1;
  }
  return {
    generated_at: options.generatedAt ?? `${FIXTURE_SITE_TODAY}T00:04:00`,
    site_today: FIXTURE_SITE_TODAY,
    tz_name: FIXTURE_TZ_NAME,
    recording_since:
      'recordingSince' in options
        ? (options.recordingSince ?? null)
        : options.firstRun
          ? null
          : '2026-09-01T03:00:00',
    history_since:
      'historySince' in options
        ? (options.historySince ?? null)
        : options.firstRun
          ? null
          : '2026-08-02T11:30:00',
    printers,
    counts_by_group: countsByGroup as FleetStatus['counts_by_group'],
    counts_by_class: countsByClass as FleetStatus['counts_by_class'],
  };
}

export const makeFleetStatusFirstRun = (): FleetStatus => makeFleetStatus({ firstRun: true });

/**
 * The live tile beside `makeFleetOverviewProduction`: a recorder that first
 * wrote an hour before the answer was built, over a fault ledger six weeks
 * deep. `2026-09-20T23:00:00` naive UTC is 11:00 on the site's 21st, one hour
 * before the window's "now".
 */
export const makeFleetStatusProduction = (): FleetStatus =>
  makeFleetStatus({
    recordingSince: '2026-09-20T23:00:00',
    historySince: '2026-08-08T06:00:00',
  });

// ── intervals ───────────────────────────────────────────────────────────────

export function makePrinterIntervals(
  overrides: Partial<PrinterIntervalsResponse> = {},
): PrinterIntervalsResponse {
  return {
    printer: { printer_id: PROBLEM_PRINTER_ID, name: '009-H2C', model: 'H2C', is_active: true, deleted: false },
    date_from: '2026-09-19',
    date_to: '2026-09-19',
    tz_name: FIXTURE_TZ_NAME,
    generated_at: `${FIXTURE_SITE_TODAY}T00:04:00`,
    intervals: [
      {
        start: '2026-09-18T12:00:00',
        end: '2026-09-18T18:30:00',
        class_key: 'printing',
        group: 'printing',
        cause: null,
        seconds: 23_400,
      },
      {
        start: '2026-09-18T18:30:00',
        end: '2026-09-18T19:02:00',
        class_key: 'cycle_overhead:cooling',
        group: 'cycle_overhead',
        cause: 'cooling',
        seconds: 1920,
      },
      {
        start: '2026-09-18T19:02:00',
        end: '2026-09-18T19:03:25',
        class_key: 'cycle_overhead:ejecting',
        group: 'cycle_overhead',
        cause: 'ejecting',
        seconds: 85,
      },
      {
        start: '2026-09-18T19:03:25',
        end: '2026-09-19T03:03:25',
        class_key: 'down:fault:jam',
        group: 'down',
        cause: 'fault:jam',
        seconds: 28_800,
      },
      {
        start: '2026-09-19T03:03:25',
        end: '2026-09-19T12:00:00',
        class_key: 'idle',
        group: 'idle',
        cause: null,
        seconds: 32_195,
      },
    ],
    incidents: [
      {
        incident_id: 51,
        kind: 'jam',
        created_at: '2026-09-18T19:03:25',
        resolved_at: '2026-09-19T03:03:25',
      },
    ],
    ...overrides,
  };
}
