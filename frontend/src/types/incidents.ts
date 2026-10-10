/**
 * Wire types for the equipment-fault ledger read (`GET /api/v1/incidents`) —
 * the TypeScript mirror of `backend/app/schemas/incidents.py`.
 *
 * This module OWNS the ledger's types: `types/fleetMetrics.ts` imports
 * `IncidentSummary` from here (the Fleet overview's recovery summary is the
 * same `printer_incidents.summary` tally), never the other way round.
 *
 * Datetimes are NAIVE UTC strings (no `Z`), as everywhere in this API; dates are
 * inclusive SITE calendar dates (`YYYY-MM-DD`).
 */

import type { PrinterIncidentKind, PrinterMessage } from '../api/client';

/** `printer_incidents.OUTCOMES` — the one derivation of how a row ended. */
export type IncidentOutcome =
  | 'recovering'
  | 'held'
  | 'auto_recovered'
  | 'human_resolved'
  | 'resolved_unpaged'
  | 'taken_over'
  | 'transient';

/**
 * Every `resolve_source` token the backend writes: the `RESOLVE_*` vocabulary
 * of `models/printer_incident.py`, plus `printer_incidents.RESOLVE_DRIVER_ENDED`
 * and the one-shot migration's `legacy_vision_stop`. The column is free text
 * and no backend registry enumerates it, so a row may carry a token this union
 * does not know — the wire field stays `string` and the label lookup
 * (`utils/incidents.resolveSourceLabelKey`) falls back to the raw token.
 */
export type IncidentResolveSource =
  | 'auto_resume'
  | 'observed_running'
  | 'terminal'
  | 'operator'
  | 'paused_elsewhere'
  | 'wire_clear'
  | 'repair_observed'
  | 'repair_completed'
  | 'driver_swap'
  | 'driver_self_heal'
  | 'driver_restart'
  // The refill driver refilled an EMPTY toolhead and its resume ran fed.
  | 'refill_resumed'
  | 'startup_rearm'
  | 'recheck_passed'
  | 'plate_refused'
  | 'handed_over'
  | 'job_ended_unseen'
  | 'driver_ended'
  | 'legacy_vision_stop';

/** `printer_incidents.summary` as a model — the equipment-fault ledger's tally. */
export interface IncidentSummary {
  /** Equipment FAULTS (declared maintenance holds excluded). */
  total: number;
  /** Faults the farm closed by its own act, never paged — `by_outcome.auto_recovered`. */
  zero_human: number;
  /** Declared maintenance holds (`service_hold`), outside `total`. */
  declared: number;
  by_outcome: Record<IncidentOutcome, number>;
  /** kind -> outcome -> count; every outcome present for each kind listed. */
  by_kind: Partial<Record<PrinterIncidentKind, Record<IncidentOutcome, number>>>;
}

/** One of the two printers a fleet-scope recurring line names. */
export interface RecurringWorstPrinter {
  printer_id: number;
  printer_name: string | null;
  holds: number;
}

/**
 * One recurring fault signature, derived at read time by
 * `printer_incidents.recurring_signatures` over the UNFILTERED window.
 * `printer` scope names one printer; `fleet` scope is one line for a code most
 * of the active roster carries, naming its `worst` printers.
 */
export interface RecurringLine {
  scope: 'printer' | 'fleet';
  printer_id: number | null;
  printer_name: string | null;
  kind: PrinterIncidentKind;
  code: string;
  printer_message: PrinterMessage | null;
  holds: number;
  /** Distinct SITE days carrying a hold. */
  days: number;
  held_s: number;
  /** Naive UTC. */
  last_at: string;
  /** The site's UTC offset AT `last_at` — the only input its site-day label needs. */
  utc_offset_minutes: number;
  fleet_median: number;
  printers_affected: number;
  roster_size: number;
  worst: RecurringWorstPrinter[];
}

/** One ledger row, with its DERIVED outcome (computed server-side, never stored). */
export interface IncidentRow {
  id: number;
  printer_id: number;
  printer_name: string | null;
  job_id: string;
  item_id: number | null;
  kind: PrinterIncidentKind;
  external: boolean;
  code: string;
  codes: string;
  slot_desc: string | null;
  status: string;
  outcome: IncidentOutcome;
  resolution_class: string;
  created_at: string;
  /** The site's UTC offset AT `created_at` — the only input its site-time label needs. */
  utc_offset_minutes: number;
  escalated_at: string | null;
  resolved_at: string | null;
  /** Free text on the wire; see `IncidentResolveSource`. */
  resolve_source: string | null;
  /** Held seconds as of the response; an open row's keeps growing. */
  held_s: number;
  /** The printer's recorded words for this fault, rendered by the backend catalog. */
  printer_messages: PrinterMessage[];
  /** Server-projected: this row matches a printer-scope recurring line. */
  recurring: boolean;
}

export interface IncidentsResponse {
  date_from: string | null;
  date_to: string | null;
  /**
   * The site zone's NAME as the host reports it — may be an OS display name
   * ("Eastern Daylight Time") that `Intl` rejects, so nothing formats with it;
   * labels read each row's `utc_offset_minutes`.
   */
  tz_name: string;
  /** Rows matching every filter across the window (all pages). */
  total: number;
  limit: number;
  offset: number;
  /** Window-wide over the kind + printer filters (the outcome filter excluded). */
  summary: IncidentSummary;
  recurring: RecurringLine[];
  items: IncidentRow[];
}

/** What the Faults tab asks `GET /incidents` for. Inclusive SITE dates. */
export interface IncidentsQuery {
  dateFrom: string;
  dateTo: string;
  kind?: PrinterIncidentKind;
  outcome?: IncidentOutcome;
  printerId?: number;
  limit: number;
  offset: number;
}
