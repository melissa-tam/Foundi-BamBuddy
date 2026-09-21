/**
 * The Fleet grid widgets' row builders — pure, i18n-free, React-free.
 *
 * Every widget draws a chart AND a data table, and the whole point of the
 * "Show data" toggle is that the two are the SAME numbers. That only holds if
 * the derivation happens once, so it happens here: a widget component picks
 * colours, labels and marks, and never computes a value.
 *
 * Three rules this module exists to keep:
 *
 * 1. **A sparse map's missing key means zero only where the bucket was
 *    observed.** The backend omits a class with no seconds, and states
 *    separately (`basis` / `observed_seconds`) whether it was watching. So the
 *    same missing key means "none" in an observed bucket and "unknown" in one
 *    the recorder never covered — `null`, which recharts draws as a GAP.
 *    A chart that read both as 0 would report a silent farm as an idle one.
 * 2. **Averages are never re-averaged and medians are never blended.** The
 *    cycle statistics pass through exactly as measured; a median of four groups
 *    is not the median of their medians, and there is no honest way to make one
 *    from what the endpoint returns.
 * 3. **Series keys are recharts `dataKey`s, so they may not contain a dot.**
 *    Recharts resolves a dotted key as a PATH (`get(row, 'SKU007.01')`), which
 *    silently yields `undefined` for a SKU code. SKU series are therefore keyed
 *    positionally (`sku_0`…, `sku_other`) with the code carried beside the key.
 *    Class groups, outcomes and down causes contain no dot and are used verbatim.
 */

import type { PrinterIncidentKind } from '../../../api/client';
import type {
  CycleGroup,
  CycleKind,
  CyclePrinterGroup,
  CycleProjection,
  FleetBucket,
  FleetCause,
  FleetGroup,
  FleetSeriesValues,
  RecoveryProjection,
  RecoveryValues,
  SeriesEnvelope,
  ThroughputValues,
  TimeToRecover,
  UnitsValues,
} from '../../../types/fleetMetrics';
import {
  DOWN_CAUSE_ORDER,
  FAULT_KIND_ORDER,
  seriesRows,
  type BucketLabel,
  type SeriesRow,
  type SeriesRowsOptions,
} from '../../../utils/fleetMetrics';

/** Seconds in an hour — the one conversion the downtime arithmetic needs. */
export const SECONDS_PER_HOUR = 3600;

/**
 * What a table cell prints where a number is not KNOWN — never where one is
 * zero. A dash and a `0` are different answers, and on a lights-out farm the
 * difference is the whole point of the tab.
 */
export const NO_VALUE = '–';

export interface RowOptions extends SeriesRowsOptions {
  /**
   * What the CURRENT bucket is called on an axis. A parameter rather than a
   * `t()` call, so every builder stays pure and a test can assert the rows
   * without standing up i18n.
   */
  todayLabel: string;
}

/** The two label forms every chart row carries. */
export interface RowLabels {
  /** The dense form an axis tick shows. */
  axisLabel: string;
  /** The unambiguous site-local form the data table's row header shows. */
  fullLabel: string;
}

export type ChartRow<Values extends object> = SeriesRow<Values> & RowLabels;

/**
 * The axis form of one bucket label.
 *
 * The month rides only the buckets that START one (`bucketLabel` decides that,
 * comparing against the previous bucket), so a 30-day axis reads
 * "Sep 3 · 4 · 5 … Oct 1 · 2" instead of repeating the month thirty times.
 */
export function bucketAxisLabel(
  label: BucketLabel,
  bucket: FleetBucket,
  todayLabel: string,
): string {
  if (label.isCurrent) return todayLabel;
  if (bucket === 'hour') return label.hour ?? label.dayOfMonth;
  return label.month === null ? label.dayOfMonth : `${label.month} ${label.dayOfMonth}`;
}

function buildRows<Values, Picked extends object>(
  envelope: SeriesEnvelope<Values>,
  pick: (values: Values, bucket: SeriesEnvelope<Values>['buckets'][number]) => Picked,
  options: RowOptions,
): ChartRow<Picked>[] {
  return seriesRows(envelope, pick, options).map((row) => ({
    ...row,
    axisLabel: bucketAxisLabel(row.bucketLabel, options.bucket, options.todayLabel),
    fullLabel: row.bucketLabel.full,
  }));
}

/** The window's elapsed seconds — the denominator every window total divides by. */
export function windowElapsedSeconds(envelope: SeriesEnvelope<unknown>): number {
  return envelope.buckets.reduce((total, bucket) => total + bucket.elapsed_seconds, 0);
}

// ── 1. printers by state over time ──────────────────────────────────────────

/**
 * One bucket of the state chart, in AVERAGE CONCURRENT PRINTERS — the same unit
 * as the live counts, which is what lets "2 down now" sit beside "2.1 down on
 * average".
 */
export interface StateRowValues {
  printing: number | null;
  cycle_overhead: number | null;
  idle: number | null;
  planned: number | null;
  down: number | null;
  /** The recorder's own blind spot, drawn as the sparse-hatch band. */
  unobserved: number | null;
  /**
   * The roster ceiling — NULL where nobody knows it.
   *
   * `printers_in_fleet` is a non-nullable float, and the backend's own identity
   * for it (`printers_known − out_of_fleet − not_recorded`) has two
   * recorder-derived terms. On a bucket the recorder never reached it therefore
   * arrives as `0.0` meaning UNKNOWN, not as a roster of nothing — and a
   * ceiling line drawn along zero for the first forty days of a window is the
   * chart stating, in its loudest channel, that the farm had no printers.
   */
  in_fleet: number | null;
  peak_down: number | null;
}

/**
 * Was the state recorder watching this bucket at all?
 *
 * `incidents_only` is the backend's own word for "the fault and hold ledger is
 * all there is here", and it is exactly `observed_seconds == 0`; both are read
 * so a response that stated only one still answers correctly.
 */
function bucketObserved(basis: string | null, observedSeconds: number): boolean {
  return basis !== 'incidents_only' && observedSeconds > 0;
}

/**
 * The two groups a bucket the recorder never reached can still state.
 *
 * An open equipment fault and a declared service hold are rows in a LEDGER:
 * durable, timestamped, and true whether or not the sampler was running. Every
 * other group is a reading the sampler had to take, so on an `incidents_only`
 * bucket it is unknown — and unknown is a gap, never a zero.
 */
const LEDGER_EVIDENCED_GROUPS: ReadonlySet<FleetGroup> = new Set<FleetGroup>(['down', 'planned']);

/**
 * One group's band for one bucket, keyed on the bucket's BASIS rather than on
 * whether the payload happened to carry the field.
 *
 * Stated as a rule and not read off the response because the two answers differ
 * exactly where it matters: a zero the recorder watched and a zero it never saw
 * arrive on the wire looking identical (a sparse map omits both), and drawing
 * the second as a band says the farm had nothing printing when the truth is
 * that nobody was counting.
 */
function classValue(
  values: FleetSeriesValues,
  group: FleetGroup,
  observed: boolean,
): number | null {
  if (!observed && !LEDGER_EVIDENCED_GROUPS.has(group)) return null;
  const value = values.avg_by_group[group];
  if (value !== undefined) return value;
  // Absent in an observed bucket means none of it happened; absent in one
  // nobody recorded means nobody knows, and a gap says so.
  return observed ? 0 : null;
}

/**
 * The no-data band: how much of the bucket is covered by nothing at all.
 *
 * On an OBSERVED bucket the backend states it (`avg_by_group.unobserved`) and
 * nothing here second-guesses it. On an `incidents_only` bucket it does not,
 * because the whole point is that it measured nothing — so the band is derived
 * from the two things that ARE known: the roster the bucket was drawn over, and
 * the ledger bands standing in front of it. Without it an unrecorded bucket
 * draws a red sliver floating over empty space, and a reader cannot tell a
 * fleet of one from a fleet of twelve with eleven unaccounted for.
 */
function noDataValue(values: FleetSeriesValues, observed: boolean): number | null {
  const stated = values.avg_by_group.unobserved;
  if (stated !== undefined) return stated;
  if (observed) return 0;
  const ledger = (values.avg_by_group.down ?? 0) + (values.avg_by_group.planned ?? 0);
  return Math.max(0, values.printers_known - ledger);
}

/**
 * The roster ceiling, or null where the bucket cannot state one.
 *
 * `printers_in_fleet` is `0.0` for "unknown" on an `incidents_only` bucket (see
 * `StateRowValues.in_fleet`), so the basis decides, not the value.
 */
function inFleetValue(values: FleetSeriesValues, observed: boolean): number | null {
  return observed ? values.printers_in_fleet : null;
}

export function stateRows(
  envelope: SeriesEnvelope<FleetSeriesValues>,
  options: RowOptions,
): ChartRow<StateRowValues>[] {
  return buildRows(
    envelope,
    (values, bucket) => {
      const observed = bucketObserved(bucket.basis, bucket.observed_seconds);
      return {
        printing: classValue(values, 'printing', observed),
        cycle_overhead: classValue(values, 'cycle_overhead', observed),
        idle: classValue(values, 'idle', observed),
        planned: classValue(values, 'planned', observed),
        down: classValue(values, 'down', observed),
        unobserved: noDataValue(values, observed),
        in_fleet: inFleetValue(values, observed),
        // A peak the incident ledger proves stands even in an unobserved
        // bucket; a zero nobody watched does not.
        peak_down: observed || values.peak_down > 0 ? values.peak_down : null,
      };
    },
    options,
  );
}

/**
 * Is there anything to draw?
 *
 * A window can have a full grid of buckets and still have nothing IN them —
 * every bucket before the recorder shipped is a row of nulls. That is an empty
 * chart, not a flat one, and it gets the empty state rather than a blank axis.
 *
 * The no-data band is deliberately NOT evidence. It is the absence itself, and
 * on a window nobody recorded it would fill every bucket to the roster line —
 * a chart that is one hundred per cent hatch, which tells a reader strictly
 * less than the sentence the empty state puts there instead. A window whose
 * only content is the fault ledger still draws, because a proven fault is a
 * fact worth a picture.
 */
export function stateHasData(rows: readonly ChartRow<StateRowValues>[]): boolean {
  return rows.some(
    (row) =>
      row.printing !== null ||
      row.cycle_overhead !== null ||
      row.idle !== null ||
      row.planned !== null ||
      row.down !== null,
  );
}

/** The window's own averages, for the table's summary row. */
export function stateTotals(envelope: SeriesEnvelope<FleetSeriesValues>): StateRowValues {
  const values = envelope.totals;
  const observed = envelope.buckets.some((bucket) =>
    bucketObserved(bucket.basis, bucket.observed_seconds),
  );
  return {
    printing: classValue(values, 'printing', observed),
    cycle_overhead: classValue(values, 'cycle_overhead', observed),
    idle: classValue(values, 'idle', observed),
    planned: classValue(values, 'planned', observed),
    down: classValue(values, 'down', observed),
    unobserved: noDataValue(values, observed),
    in_fleet: inFleetValue(values, observed),
    peak_down: observed || values.peak_down > 0 ? values.peak_down : null,
  };
}

// ── 2. prints per day ───────────────────────────────────────────────────────

/**
 * Print counts come from the print log, which is COMPLETE for its own history —
 * a recorder gap says nothing about whether a print finished — so a missing
 * outcome is always a zero here, never a gap.
 */
export interface PrintsRowValues {
  completed: number;
  failed: number;
  cancelled: number;
  other: number;
  total: number;
  /** A rate, not a count: completed prints ÷ printer-days in service. */
  per_printer: number | null;
}

const pickPrints = (values: ThroughputValues): PrintsRowValues => ({
  completed: values.by_outcome.completed ?? 0,
  failed: values.by_outcome.failed ?? 0,
  cancelled: values.by_outcome.cancelled ?? 0,
  other: values.by_outcome.other ?? 0,
  total: values.total,
  per_printer: values.prints_per_printer_per_day,
});

export function printsRows(
  envelope: SeriesEnvelope<ThroughputValues>,
  options: RowOptions,
): ChartRow<PrintsRowValues>[] {
  return buildRows(envelope, pickPrints, options);
}

export function printsTotals(envelope: SeriesEnvelope<ThroughputValues>): PrintsRowValues {
  return pickPrints(envelope.totals);
}

// ── 3. downtime by cause ────────────────────────────────────────────────────

/** Hours down per cause for one bucket, plus the bucket's own total. */
export type DowntimeRowValues = { total_hours: number } & Partial<Record<FleetCause, number>>;

/**
 * The causes actually present in this window, in the actionable-first order.
 *
 * Collected across every bucket, not off `totals`, so a cause that appeared in
 * exactly one bucket still gets its band — and so the identity below (the cause
 * bands sum to the total) can never be short a term.
 */
export function downtimeCauses(envelope: SeriesEnvelope<FleetSeriesValues>): FleetCause[] {
  const present = new Set<string>();
  for (const bucket of envelope.buckets) {
    for (const [cause, value] of Object.entries(bucket.values.avg_down_by_cause)) {
      if ((value ?? 0) > 0) present.add(cause);
    }
  }
  for (const [cause, value] of Object.entries(envelope.totals.avg_down_by_cause)) {
    if ((value ?? 0) > 0) present.add(cause);
  }
  const known = DOWN_CAUSE_ORDER.filter((cause) => present.has(cause));
  // A cause this build does not know about still gets a band rather than being
  // silently dropped out of a total the reader is about to add up.
  const unknown = [...present]
    .filter((cause) => !DOWN_CAUSE_ORDER.includes(cause as FleetCause))
    .sort() as FleetCause[];
  return [...known, ...unknown];
}

/**
 * Average concurrent printers down × the bucket's ELAPSED hours = hours down.
 *
 * The product of "printers" and "hours" is a printer-hour, and that is exactly
 * the figure a reader wants ("the fleet lost four hours yesterday"); the WORD
 * never reaches the surface — the axis says hours down. Elapsed, never the
 * bucket's width, so today's half-finished day is not reported as a quiet one.
 */
function pickDowntime(
  values: FleetSeriesValues,
  elapsedSeconds: number,
  causes: readonly FleetCause[],
): DowntimeRowValues {
  const hours = elapsedSeconds / SECONDS_PER_HOUR;
  const row: DowntimeRowValues = { total_hours: values.avg_down * hours };
  for (const cause of causes) {
    row[cause] = (values.avg_down_by_cause[cause] ?? 0) * hours;
  }
  return row;
}

export function downtimeRows(
  envelope: SeriesEnvelope<FleetSeriesValues>,
  causes: readonly FleetCause[],
  options: RowOptions,
): ChartRow<DowntimeRowValues>[] {
  return buildRows(
    envelope,
    (values, bucket) => pickDowntime(values, bucket.elapsed_seconds, causes),
    options,
  );
}

export function downtimeTotals(
  envelope: SeriesEnvelope<FleetSeriesValues>,
  causes: readonly FleetCause[],
): DowntimeRowValues {
  return pickDowntime(envelope.totals, windowElapsedSeconds(envelope), causes);
}

// ── 4. cooling and eject ────────────────────────────────────────────────────

/**
 * One measured group's durations, straight from the episode ledger.
 *
 * Every statistic is copied, never combined: `not_completed` stays a count
 * beside the percentiles rather than being folded into them (an eject that was
 * stopped has no duration to contribute), and `count` rides the row so a median
 * of two episodes is never read as a fleet fact.
 */
export interface CycleRow {
  id: string;
  kind: CycleKind;
  model: string | null;
  variant: string | null;
  /** Set only in the per-printer view. */
  printerId: number | null;
  count: number;
  medianS: number | null;
  p90S: number | null;
  notCompleted: number;
  overExpected: number | null;
  overExpectedShare: number | null;
}

/** Cooling before ejecting, because that is the order the machine does them in. */
const CYCLE_KIND_ORDER: readonly CycleKind[] = ['cooldown', 'eject'];

const byText = (left: string, right: string): number => (left < right ? -1 : left > right ? 1 : 0);

export interface CycleRowOptions {
  /** Read `by_printer` instead of `by_model` — the widget's own toggle. */
  perPrinter: boolean;
}

/** A guard, not a bare `in`: the union's wider member does not declare the field. */
const isPerPrinter = (group: CycleGroup | CyclePrinterGroup): group is CyclePrinterGroup =>
  'printer_id' in group;

export function cycleRows(
  projection: CycleProjection,
  { perPrinter }: CycleRowOptions,
): CycleRow[] {
  const source: readonly (CycleGroup | CyclePrinterGroup)[] = perPrinter
    ? projection.by_printer
    : projection.by_model;
  return source
    .map((group): CycleRow => {
      const printerId = isPerPrinter(group) ? group.printer_id : null;
      return {
        id: `${printerId ?? 'model'}:${group.kind}:${group.model ?? ''}:${group.variant ?? ''}`,
        kind: group.kind,
        model: group.model,
        variant: group.variant,
        printerId,
        count: group.stats.count,
        medianS: group.stats.median_s,
        p90S: group.stats.p90_s,
        notCompleted: group.stats.not_completed,
        overExpected: group.stats.over_expected,
        overExpectedShare: group.stats.over_expected_share,
      };
    })
    .sort((left, right) => {
      const kind = CYCLE_KIND_ORDER.indexOf(left.kind) - CYCLE_KIND_ORDER.indexOf(right.kind);
      if (kind !== 0) return kind;
      const printer = (left.printerId ?? 0) - (right.printerId ?? 0);
      if (printer !== 0) return printer;
      const model = byText(left.model ?? '', right.model ?? '');
      return model !== 0 ? model : byText(left.variant ?? '', right.variant ?? '');
    });
}

/** Episodes that never reached a finished outcome, over all the rows shown. */
export function cycleNotCompleted(rows: readonly CycleRow[]): number {
  return rows.reduce((total, row) => total + row.notCompleted, 0);
}

// ── 5. recovery ─────────────────────────────────────────────────────────────

/** Incidents OPENED in a bucket — an arrival rate, not a standing count. */
export type RecoveryRowValues = { opened: number } & Partial<Record<PrinterIncidentKind, number>>;

/**
 * The incident kinds present in the window: the fault kinds in the store's own
 * precedence order first, then anything else the ledger carried (a declared
 * `service_hold` is not a fault, and is not sorted among them).
 */
export function recoveryKinds(
  series: SeriesEnvelope<RecoveryValues>,
): PrinterIncidentKind[] {
  const present = new Set<string>();
  for (const bucket of series.buckets) {
    for (const [kind, count] of Object.entries(bucket.values.opened_by_kind)) {
      if ((count ?? 0) > 0) present.add(kind);
    }
  }
  const faults = FAULT_KIND_ORDER.filter((kind) => present.has(kind)) as PrinterIncidentKind[];
  const rest = [...present]
    .filter((kind) => !FAULT_KIND_ORDER.includes(kind as (typeof FAULT_KIND_ORDER)[number]))
    .sort() as PrinterIncidentKind[];
  return [...faults, ...rest];
}

function pickRecovery(
  values: RecoveryValues,
  kinds: readonly PrinterIncidentKind[],
): RecoveryRowValues {
  const row: RecoveryRowValues = { opened: values.opened };
  for (const kind of kinds) row[kind] = values.opened_by_kind[kind] ?? 0;
  return row;
}

export function recoveryRows(
  series: SeriesEnvelope<RecoveryValues>,
  kinds: readonly PrinterIncidentKind[],
  options: RowOptions,
): ChartRow<RecoveryRowValues>[] {
  return buildRows(series, (values) => pickRecovery(values, kinds), options);
}

export function recoveryTotals(
  series: SeriesEnvelope<RecoveryValues>,
  kinds: readonly PrinterIncidentKind[],
): RecoveryRowValues {
  return pickRecovery(series.totals, kinds);
}

/** The three figures the Recovery widget leads with. */
export interface RecoveryHeadline {
  /** Incident-held hours clipped to the window. Deliberately not hours DOWN. */
  faultOpenHours: number;
  /** Share of incidents that closed without anybody touching the printer. */
  clearedNoPersonShare: number | null;
  /**
   * The kind that arrived most often, and its OWN median — never a median of
   * medians, which is what a single blended "time to recover" would have to be.
   */
  leadRecovery: TimeToRecover | null;
}

export function recoveryHeadline(projection: RecoveryProjection): RecoveryHeadline {
  const ranked = [...projection.time_to_recover]
    .filter((entry) => entry.count > 0)
    .sort((left, right) => right.count - left.count || byText(left.kind, right.kind));
  return {
    faultOpenHours: projection.fault_open_seconds / SECONDS_PER_HOUR,
    clearedNoPersonShare:
      projection.summary.total > 0 ? projection.summary.zero_human / projection.summary.total : null,
    leadRecovery: ranked[0] ?? null,
  };
}

// ── 6. parts by SKU ─────────────────────────────────────────────────────────

/**
 * How many SKUs get their own band before the rest fold into "other".
 *
 * Five, because this widget's default size is half the grid (~460 px at the
 * 1280 layout) and its legend sits under the plot: six entries fit on two lines
 * there and stay legible when the sidebar is expanded. A sixth band would also
 * push the series past the colours this palette owns.
 */
export const SKU_SERIES_LIMIT = 5;

/** One band of the SKU chart. `sku === null` is the "other" remainder. */
export interface SkuSeries {
  /** The recharts `dataKey`. Positional, because a SKU code contains a dot. */
  key: string;
  sku: string | null;
}

export const SKU_OTHER_KEY = 'sku_other';

/**
 * The top SKUs by WINDOW total, plus an "other" band when anything is left.
 *
 * Ranked on the window rather than per bucket so a band keeps its colour and
 * its position for the whole chart — a series that changed identity between
 * Tuesday and Wednesday would make the stack unreadable.
 */
export function skuSeries(
  envelope: SeriesEnvelope<UnitsValues>,
  limit: number = SKU_SERIES_LIMIT,
): SkuSeries[] {
  const ranked = Object.entries(envelope.totals.by_sku)
    .filter(([, units]) => units > 0)
    .sort((left, right) => right[1] - left[1] || byText(left[0], right[0]));
  const top = ranked.slice(0, limit);
  const series: SkuSeries[] = top.map(([sku], index) => ({ key: `sku_${index}`, sku }));
  const namedTotal = top.reduce((total, [, units]) => total + units, 0);
  if (ranked.length > limit || envelope.totals.units > namedTotal) {
    series.push({ key: SKU_OTHER_KEY, sku: null });
  }
  return series;
}

/** Units per band for one bucket. `units` is the bucket's own total. */
export interface PartsRowValues {
  units: number;
  [seriesKey: string]: number;
}

function pickParts(values: UnitsValues, series: readonly SkuSeries[]): PartsRowValues {
  const row: PartsRowValues = { units: values.units };
  let named = 0;
  for (const entry of series) {
    if (entry.sku === null) continue;
    const units = values.by_sku[entry.sku] ?? 0;
    row[entry.key] = units;
    named += units;
  }
  const other = series.find((entry) => entry.sku === null);
  // The remainder is SUBTRACTED from the bucket's own total rather than summed
  // from the SKUs left over, so the bands always add up to the figure the table
  // prints even when a code is missing from `by_sku`.
  if (other) row[other.key] = Math.max(0, values.units - named);
  return row;
}

export function partsRows(
  envelope: SeriesEnvelope<UnitsValues>,
  series: readonly SkuSeries[],
  options: RowOptions,
): ChartRow<PartsRowValues>[] {
  return buildRows(envelope, (values) => pickParts(values, series), options);
}

export function partsTotals(
  envelope: SeriesEnvelope<UnitsValues>,
  series: readonly SkuSeries[],
): PartsRowValues {
  return pickParts(envelope.totals, series);
}
