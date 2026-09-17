/**
 * "Does Recover apply to this printer, and what will it do" — the ONE origin.
 *
 * Recover (`POST /printers/{id}/recover`) is the operator's override for
 * everything the farm is holding a printer by: the plate gate, a dispatch lease,
 * an in-flight eject claim, a quarantine, and — since the incident-resolution
 * wave — an open equipment fault whose rule names Recover as its exit. Before
 * this module each surface decided for itself which of those counted, so a
 * printer held ONLY by an escalated fault (011-H2S, row 188) had no card
 * affordance at all while the backend verb sat there able to close it.
 *
 * The card menu, the quarantine banner, the stalled-eject banner and the confirm
 * dialog all read `recoverEffects` — the dialog renders one line per effect, so
 * "what the operator is told" and "what makes the verb reachable" can never drift
 * apart again.
 *
 * `equipment_fault` is `open_incident.operator_exits`, the BACKEND's verdict
 * (`printer_incidents.closed_by_recover`). It is never re-derived from `kind`
 * here: a `z_reference_lost` hold raises no gate and still exits on Recover,
 * while a `runout` waits for the wire — same chip, opposite answer.
 */
import type { Printer, PlateEjectClaim, PrinterStatus } from '../api/client';

/**
 * Everything Recover clears, in the order the confirm dialog lists it: what the
 * authority holds, then the farm-policy holds.
 */
export const RECOVER_EFFECTS = ['plate', 'lease', 'eject', 'quarantine', 'equipment_fault'] as const;

export type RecoverEffect = (typeof RECOVER_EFFECTS)[number];

/**
 * The in-flight eject claim on this printer, or null. The stalled-eject banner
 * reads the claim through here (for `runtime_exceeded` and the age) so it keys
 * off the same record the `eject` effect does.
 */
export function inFlightEject(status: PrinterStatus | undefined): PlateEjectClaim | null {
  return status?.occupancy?.eject ?? null;
}

/** One predicate per effect — a `Record` so a new effect cannot be forgotten. */
const EFFECT_PRESENT: Record<
  RecoverEffect,
  (printer: Printer, status: PrinterStatus | undefined) => boolean
> = {
  plate: (_printer, status) => status?.occupancy?.plate.occupied === true,
  lease: (_printer, status) => status?.occupancy?.lease_age_s != null,
  eject: (_printer, status) => inFlightEject(status) !== null,
  quarantine: (printer) => printer.quarantined === true,
  equipment_fault: (_printer, status) => status?.open_incident?.operator_exits === true,
};

/** What Recover would clear on this printer right now, `RECOVER_EFFECTS`-ordered. */
export function recoverEffects(
  printer: Printer,
  status: PrinterStatus | undefined,
): RecoverEffect[] {
  return RECOVER_EFFECTS.filter((effect) => EFFECT_PRESENT[effect](printer, status));
}

/** Whether the verb is offered at all: it is, exactly when it would do something. */
export function recoverApplies(effects: readonly RecoverEffect[]): boolean {
  return effects.length > 0;
}
