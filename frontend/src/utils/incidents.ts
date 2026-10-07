/**
 * THE owner of the fault ledger's words and derived figures on the client:
 * outcome labels and badge colours, the "closed by" label of a resolve source,
 * an open row's live held time, and the cleared-with-no-person share the Fleet
 * tab's Recovery headline and the Faults tab's summary strip both state.
 *
 * Fault DESCRIPTIONS are not here: they arrive rendered by the backend catalog
 * (`printer_messages` / `printer_message`). The Faults surfaces print the
 * catalog's `description` only when it is non-empty — the code is always on
 * screen beside it, so the code-as-fallback of `printerMessageText` would print
 * it twice. No code→text table on the client.
 */

import type {
  IncidentOutcome,
  IncidentResolveSource,
  IncidentRow,
  IncidentSummary,
} from '../types/incidents';

/** `printer_incidents.OUTCOMES`, in the backend's order. */
export const INCIDENT_OUTCOMES = [
  'recovering',
  'held',
  'auto_recovered',
  'human_resolved',
  'resolved_unpaged',
  'taken_over',
  'transient',
] as const satisfies readonly IncidentOutcome[];

/** The two outcomes of a row that is still open. */
const OPEN_OUTCOMES: ReadonlySet<IncidentOutcome> = new Set<IncidentOutcome>(['recovering', 'held']);

export function isOpenOutcome(outcome: IncidentOutcome): boolean {
  return OPEN_OUTCOMES.has(outcome);
}

export function outcomeLabelKey(outcome: IncidentOutcome): string {
  return `incidents.outcome.${outcome}`;
}

/**
 * Badge colours, from the queue status chip's palette: held = the error tone,
 * recovering = the warning tone, auto recovered = the ok tone, a human's close
 * neutral, an abort muted.
 */
const OUTCOME_BADGE_CLASS: Record<IncidentOutcome, string> = {
  held: 'text-status-error bg-status-error/10 border-status-error/20',
  recovering: 'text-status-warning bg-status-warning/10 border-status-warning/20',
  auto_recovered: 'text-status-ok bg-status-ok/10 border-status-ok/20',
  human_resolved: 'text-blue-700 dark:text-blue-400 bg-blue-400/10 border-blue-400/20',
  resolved_unpaged: 'text-blue-700 dark:text-blue-400 bg-blue-400/10 border-blue-400/20',
  taken_over: 'text-gray-600 dark:text-gray-400 bg-gray-400/10 border-gray-400/20',
  transient: 'text-gray-600 dark:text-gray-400 bg-gray-400/10 border-gray-400/20',
};

export function outcomeBadgeClass(outcome: IncidentOutcome): string {
  return OUTCOME_BADGE_CLASS[outcome];
}

/** The label leaf per resolve source. A `Record`, so a token added to the union without a leaf fails `tsc -b`. */
export const RESOLVE_SOURCE_LABEL_KEY: Record<IncidentResolveSource, string> = {
  auto_resume: 'incidents.closedBy.auto_resume',
  observed_running: 'incidents.closedBy.observed_running',
  terminal: 'incidents.closedBy.terminal',
  operator: 'incidents.closedBy.operator',
  paused_elsewhere: 'incidents.closedBy.paused_elsewhere',
  wire_clear: 'incidents.closedBy.wire_clear',
  repair_observed: 'incidents.closedBy.repair_observed',
  repair_completed: 'incidents.closedBy.repair_completed',
  driver_swap: 'incidents.closedBy.driver_swap',
  driver_self_heal: 'incidents.closedBy.driver_self_heal',
  driver_restart: 'incidents.closedBy.driver_restart',
  startup_rearm: 'incidents.closedBy.startup_rearm',
  recheck_passed: 'incidents.closedBy.recheck_passed',
  plate_refused: 'incidents.closedBy.plate_refused',
  handed_over: 'incidents.closedBy.handed_over',
  job_ended_unseen: 'incidents.closedBy.job_ended_unseen',
  driver_ended: 'incidents.closedBy.driver_ended',
  legacy_vision_stop: 'incidents.closedBy.legacy_vision_stop',
};

/** The leaf a row with no resolve source reads. */
export const NO_RESOLVE_SOURCE_LABEL_KEY = 'incidents.closedBy.none';

function isResolveSource(source: string): source is IncidentResolveSource {
  return Object.prototype.hasOwnProperty.call(RESOLVE_SOURCE_LABEL_KEY, source);
}

/**
 * What the "Closed by" cell says: a label leaf, or the raw token when the
 * backend wrote one this client does not know (`resolve_source` is free text).
 */
export type ResolveSourceLabel = { key: string } | { raw: string };

export function resolveSourceLabelKey(source: string | null): ResolveSourceLabel {
  if (source === null || source === '') return { key: NO_RESOLVE_SOURCE_LABEL_KEY };
  return isResolveSource(source) ? { key: RESOLVE_SOURCE_LABEL_KEY[source] } : { raw: source };
}

/**
 * An open row's held time NOW: the server's `held_s` plus the clock's advance
 * since the response landed (`dataUpdatedAt`). An advance, not a second
 * derivation — a closed row's figure is returned untouched, and a clock that
 * reads earlier than the response adds nothing.
 */
export function liveHeldSeconds(
  row: Pick<IncidentRow, 'held_s' | 'outcome'>,
  dataUpdatedAt: number,
  now: number,
): number {
  if (!isOpenOutcome(row.outcome)) return row.held_s;
  return row.held_s + Math.max(0, now - dataUpdatedAt) / 1000;
}

/**
 * Share of faults that closed with no person involved (`zero_human / total`),
 * or `null` when there were no faults. ONE spelling, read by the Fleet tab's
 * Recovery headline and the Faults tab's summary strip.
 */
export function clearedNoPersonShare(summary: Pick<IncidentSummary, 'total' | 'zero_human'>): number | null {
  return summary.total > 0 ? summary.zero_human / summary.total : null;
}
