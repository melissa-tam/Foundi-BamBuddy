// The printer-card incident chip's tooltip: the instruction the hold asks for,
// looked up per kind (`printers.incidentAction.<kind>`) — the compiler cannot
// see these dynamic keys, so `__tests__/i18n/incidentKinds.test.ts` pins them.
import type { OpenIncidentState, PrinterIncidentKind } from '../api/client';

/**
 * Kinds whose `incidentAction` names a PERSON's exits that do not exist while
 * the farm still acts, so the chip shows `printers.incidentRecoveringAction.<kind>`
 * until the row is `escalated`.
 *
 * `plate_vision`: the farm re-checks and stop-and-retries first; "Ignore and
 * resume" appears only on the person's turn (`PrinterStatus.plate_check_exit`),
 * and the backend answers an earlier press with HTTP 409.
 *
 * Every other kind keeps its one instruction in both states: a `runout` row is
 * `recovering` while it waits for the operator's refill, so its instruction is
 * what the operator needs.
 */
export const RECOVERING_ACTION_KINDS: readonly PrinterIncidentKind[] = ['plate_vision'];

/** The i18n key of the chip's tooltip for this hold, in its current state. */
export function incidentActionKey(incident: Pick<OpenIncidentState, 'kind' | 'status'>): string {
  return incident.status !== 'escalated' && RECOVERING_ACTION_KINDS.includes(incident.kind)
    ? `printers.incidentRecoveringAction.${incident.kind}`
    : `printers.incidentAction.${incident.kind}`;
}
