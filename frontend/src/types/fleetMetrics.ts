/**
 * Wire types for the fleet-metrics read API — the TypeScript mirror of
 * `backend/app/schemas/fleet_metrics.py`.
 *
 * Field names, nullability and shapes follow the pydantic models EXACTLY: this
 * module is a transcription, not a design. Anything that reshapes a response
 * for a chart belongs in `utils/fleetMetrics.ts`; anything that fetches one
 * belongs in `hooks/useFleetMetrics.ts`.
 *
 * Two transcription rules worth stating, because getting either wrong is silent:
 *
 * - **Datetimes are strings.** The backend's convention is NAIVE UTC, so the
 *   JSON carries no `Z`. Nothing here parses one with the browser's timezone:
 *   a bucket renders its SITE-local label from its own `utc_offset_minutes`
 *   (see `utils/fleetMetrics.seriesRows`), which is why the Fleet tab reads the
 *   same in Kiritimati and in Los Angeles.
 * - **`dict[int, …]` becomes `Record<string, …>`.** JSON object keys are always
 *   strings, so a printer-keyed map arrives keyed by the id's decimal spelling.
 *
 * **Sparse maps mean zero, never unknown.** A class or outcome with no seconds
 * is omitted. Absence of DATA travels in `observed_seconds` and `basis`
 * instead — the distinction the matrix renders as four different cells.
 */

import type { PrinterIncidentKind } from '../api/client';

// ── the class vocabulary ────────────────────────────────────────────────────
//
// Literal unions, never free strings: a typo in a class key is a compile error
// here and a silently missing chart band anywhere else. Mirrors
// `backend/app/services/fleet_metrics/classifier.py`.

/** The coarse answer every headline figure is stated in. */
export type FleetGroup =
  | 'printing'
  | 'cycle_overhead'
  | 'idle'
  | 'down'
  | 'planned'
  | 'out_of_fleet'
  | 'not_recorded'
  | 'unobserved';

/** The two plate phases that ARE the farm's own between-prints work. */
export type FleetCycleCause = 'cooling' | 'ejecting';

/** The observed conditions that stop work, other than an open equipment fault. */
export type FleetConditionCause =
  | 'offline'
  | 'paused'
  | 'quarantined'
  | 'plate_held'
  | 'model_mismatch'
  | 'no_usb';

/**
 * The incident kinds that count as an equipment FAULT.
 *
 * Derived from the union the printer surfaces already use rather than
 * re-spelled: the backend's `FAULT_KINDS` is `ALL_KINDS - DECLARED_KINDS`, and
 * `service_hold` is the only declared kind. A kind added to `PrinterIncidentKind`
 * therefore joins this union in the same change.
 */
export type FleetFaultKind = Exclude<PrinterIncidentKind, 'service_hold'>;

/** An equipment fault names its kind after the prefix: `fault:jam`. */
export type FleetFaultCause = `fault:${FleetFaultKind}`;

/** Every cause the classifier can attach to a group. */
export type FleetCause = FleetCycleCause | FleetConditionCause | FleetFaultCause;

/**
 * The stable key a group/cause pair projects to — `group` alone when the group
 * has only one way to be true, `group:cause` otherwise. The keys of every
 * sparse class map.
 */
export type FleetClassKey =
  | Exclude<FleetGroup, 'cycle_overhead' | 'down'>
  | `cycle_overhead:${FleetCycleCause}`
  | `down:${FleetConditionCause}`
  | `down:${FleetFaultCause}`;

/** Bucket width the server chose for a window. Echoed on every response. */
export type FleetBucket = 'hour' | 'day' | 'week';

/**
 * What the STATE figures in a bucket rest on. `incidents_only` means the
 * recorder covered none of it and the fault/hold ledger is all there is — every
 * ratio is null there, and the matrix hatches the cell.
 */
export type FleetBasis = 'observed' | 'incidents_only';

/** The outcome buckets `services/print_log.outcome_bucket` folds a status into. */
export type PrintOutcome = 'completed' | 'failed' | 'cancelled' | 'other';

/** `farm_cycle_episode.kind`. */
export type CycleKind = 'cooldown' | 'eject';

/** A cooldown's measured variant — the variable that separates cooldown times. */
export type CooldownVariant = 'hold' | 'fan_only';

// ── series envelope ─────────────────────────────────────────────────────────

/** One cell of a series: its header, then whatever that series measures. */
export interface SeriesBucket<Values> {
  /** Naive UTC, no `Z`. */
  start: string;
  /** The bucket's width. */
  seconds: number;
  /** Width clamped to "now". EVERY average and rate in `values` divides by this. */
  elapsed_seconds: number;
  /** How much of the bucket the state recorder covered. Below elapsed = hatch. */
  observed_seconds: number;
  /** The site's UTC offset AT this instant — the only input a site-local label needs. */
  utc_offset_minutes: number;
  /** Null on a series whose source is not the state recorder. */
  basis: FleetBasis | null;
  values: Values;
}

/** A whole series: its buckets, and the same shape re-aggregated over the window. */
export interface SeriesEnvelope<Values> {
  buckets: SeriesBucket<Values>[];
  totals: Values;
}

/** Who a row is about. `name` falls back to the printer's id once it is deleted. */
export interface PrinterRef {
  printer_id: number;
  name: string;
  model: string | null;
  is_active: boolean;
  /** The printer row is gone; the history about it is not. */
  deleted: boolean;
}

// ── fleet series ────────────────────────────────────────────────────────────

/** The fleet as AVERAGE CONCURRENT PRINTERS — the same unit as the live counts. */
export interface FleetSeriesValues {
  printers_known: number;
  printers_in_fleet: number;
  /** class GROUP -> average concurrent printers. Sparse. */
  avg_by_group: Partial<Record<FleetGroup, number>>;
  /** down CAUSE -> average concurrent printers. Sparse. Sums to `avg_down`. */
  avg_down_by_cause: Partial<Record<FleetCause, number>>;
  avg_down: number;
  /** The most printers down AT ONCE inside the bucket. */
  peak_down: number;
  /** Null when nothing was counted, and on an `incidents_only` bucket. */
  uptime: number | null;
  time_printing: number | null;
}

// ── matrix ──────────────────────────────────────────────────────────────────

/** One printer's (or the fleet's) bucket: where its time went, and what it made. */
export interface MatrixCell {
  /** class KEY -> seconds. Sparse. Sums to the bucket's `elapsed_seconds`. */
  class_seconds: Partial<Record<FleetClassKey, number>>;
  down_seconds: number;
  /** outcome bucket -> print count. Sparse. */
  prints: Partial<Record<PrintOutcome, number>>;
  basis: FleetBasis;
}

/** Every printer's cell for one bucket, plus the fleet row under them. */
export interface MatrixValues {
  /** Keyed by the printer id's decimal spelling (JSON object keys are strings). */
  printers: Record<string, MatrixCell>;
  fleet: MatrixCell;
}

/** A matrix row's identity and the two per-day figures shown beside the name. */
export interface MatrixPrinter extends PrinterRef {
  /** Null before this printer had any counted time — a rate with no denominator. */
  hours_down_per_day: number | null;
  prints_per_day: number | null;
}

/** The matrix: a roster dimension, and the series indexed by it. */
export interface MatrixProjection {
  printers: MatrixPrinter[];
  series: SeriesEnvelope<MatrixValues>;
}

// ── throughput and units ────────────────────────────────────────────────────

/** Prints, from the print log — complete for its own history, so no `basis`. */
export interface ThroughputValues {
  by_outcome: Partial<Record<PrintOutcome, number>>;
  total: number;
  /** printer id (as a string) -> outcome bucket -> count. Sparse both levels. */
  by_printer: Record<string, Partial<Record<PrintOutcome, number>>>;
  prints_per_day: number | null;
  prints_per_printer_per_day: number | null;
  /** completed ÷ (completed + failed). Cancelled prints are excluded. */
  success_pct: number | null;
}

/** Sellable units, from completed queue plates × the SKU file's units per plate. */
export interface UnitsValues {
  units: number;
  plates: number;
  /** SKU code -> units. Sparse. */
  by_sku: Record<string, number>;
}

// ── cycle episodes ──────────────────────────────────────────────────────────

/** One group's measured durations. Percentiles describe the FINISHED episodes. */
export interface CycleStats {
  count: number;
  median_s: number | null;
  p90_s: number | null;
  /** Episodes that did not reach their kind's finished outcome. Never in the percentiles. */
  not_completed: number;
  /** Ejects only — a cooldown ends at a temperature and has nothing to be late against. */
  over_expected: number | null;
  over_expected_share: number | null;
}

/** Durations for one `kind` × printer model × variant. */
export interface CycleGroup {
  kind: CycleKind;
  model: string | null;
  /** A cooldown's `hold` / `fan_only`; an eject's purpose. Null when ungrouped. */
  variant: string | null;
  stats: CycleStats;
}

/** The same, for one printer — the toggle's data, returned with the group view. */
export interface CyclePrinterGroup extends CycleGroup {
  printer_id: number;
}

/** Cooling and eject durations, grouped both ways so the UI toggle needs no request. */
export interface CycleProjection {
  by_model: CycleGroup[];
  by_printer: CyclePrinterGroup[];
}

// ── recovery ────────────────────────────────────────────────────────────────

/** Incidents OPENED in the bucket — the arrival rate, not the standing count. */
export interface RecoveryValues {
  opened: number;
  /** incident kind -> count. Sparse. */
  opened_by_kind: Partial<Record<PrinterIncidentKind, number>>;
}

/** `printer_incidents.summary` as a model — the equipment-fault ledger's tally. */
export interface IncidentSummary {
  total: number;
  zero_human: number;
  declared: number;
  by_outcome: Record<string, number>;
  by_kind: Record<string, Record<string, number>>;
}

/** How long one kind's holds stood, over the FAULT rows opened in the window. */
export interface TimeToRecover {
  kind: PrinterIncidentKind;
  count: number;
  open_count: number;
  total_held_s: number;
  median_recover_s: number | null;
  p90_recover_s: number | null;
}

/** Faults and holds: when they arrived, how long they took, how many held hours. */
export interface RecoveryProjection {
  series: SeriesEnvelope<RecoveryValues>;
  summary: IncidentSummary;
  time_to_recover: TimeToRecover[];
  /** Incident-held seconds CLIPPED to the window — deliberately not hours DOWN. */
  fault_open_seconds: number;
}

// ── summary ─────────────────────────────────────────────────────────────────

/** The summary rows the backend composes, in the order it composes them. */
export type SummaryRowKey =
  | 'avg_printing'
  | 'avg_cycle_overhead'
  | 'avg_idle'
  | 'avg_down'
  | 'avg_planned'
  | 'peak_down'
  | 'printers_in_fleet'
  | 'prints_per_day'
  | 'prints_per_printer_per_day'
  | 'uptime'
  | 'time_printing';

/** One headline: this window's figure, the previous window's, and the sparkline. */
export interface SummaryRow {
  key: SummaryRowKey;
  /** Null when the window has no observed bucket and the row is state-derived. */
  figure: number | null;
  previous: number | null;
  /** One value per bucket of the SAME grid the other series use. */
  series: (number | null)[];
}

export interface SummaryProjection {
  rows: SummaryRow[];
}

// ── drill-down and live status ──────────────────────────────────────────────

/** One stretch of one printer's window, as the drill-down lists it. */
export interface ClassifiedInterval {
  start: string;
  end: string;
  class_key: FleetClassKey;
  group: FleetGroup;
  cause: FleetCause | null;
  seconds: number;
}

/** An incident overlapping the drill-down's range, for the same list. */
export interface IncidentInterval {
  incident_id: number;
  kind: PrinterIncidentKind;
  created_at: string;
  resolved_at: string | null;
}

/** One printer's classified intervals — what a matrix cell was summed from. */
export interface PrinterIntervalsResponse {
  printer: PrinterRef;
  /** Inclusive site date, `YYYY-MM-DD`. */
  date_from: string;
  date_to: string;
  tz_name: string;
  generated_at: string;
  intervals: ClassifiedInterval[];
  incidents: IncidentInterval[];
}

/** One printer RIGHT NOW, through the same classifier the history is read with. */
export interface PrinterStatus extends PrinterRef {
  class_key: FleetClassKey;
  group: FleetGroup;
  cause: FleetCause | null;
  /** When the current class began. */
  since: string | null;
  /** The walk back hit its bound, so `since` is the earliest instant proven. */
  since_open_ended: boolean;
}

/** The live tile: every printer's class now, and what "all time" resolves to. */
export interface FleetStatus {
  generated_at: string;
  /** The site's calendar date, `YYYY-MM-DD` — the Fleet tab's timeframe anchor. */
  site_today: string;
  tz_name: string;
  /** The first observation ever recorded. State history cannot predate it. */
  recording_since: string | null;
  /** The earlier of that and the first incident — what "all time" resolves to. */
  history_since: string | null;
  printers: PrinterStatus[];
  counts_by_group: Partial<Record<FleetGroup, number>>;
  counts_by_class: Partial<Record<FleetClassKey, number>>;
}

/** ONE history response: every projection over one window, from one timeline. */
export interface FleetOverview {
  date_from: string;
  date_to: string;
  /** Echoed, because the client omits it and the server chooses from the window. */
  bucket: FleetBucket;
  tz_name: string;
  generated_at: string;
  window_start: string;
  window_end: string;
  summary: SummaryProjection;
  matrix: MatrixProjection;
  fleet_series: SeriesEnvelope<FleetSeriesValues>;
  throughput: SeriesEnvelope<ThroughputValues>;
  units: SeriesEnvelope<UnitsValues>;
  cycle: CycleProjection;
  recovery: RecoveryProjection;
}
