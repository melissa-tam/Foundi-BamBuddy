/**
 * Farm cooldown/eject phase derivation (Phase 4.3c).
 *
 * The loop between two units looks idle without this: the printer sits in
 * FINISH with the plate gate raised while the server-side cooldown watch waits
 * for the bed to reach the release threshold. `eject_watch` (from printer
 * status) carries that threshold while the watch is armed. One pure function
 * shared by the printer card and the run detail page — derived on render,
 * never stored.
 *
 * The physical sweep itself is not separately observable — the server
 * dispatches the eject job once the bed reaches the release threshold, so it is
 * deliberately folded into the cooling phase rather than shown as its own.
 */

export interface FarmPhaseInput {
  /** Raw printer gcode_state (RUNNING / PAUSE / FINISH / FAILED / IDLE…). */
  state?: string | null;
  awaiting_plate_clear?: boolean;
  eject_watch?: { threshold_c: number } | null;
}

export type FarmPhase =
  | { kind: 'printing' }
  | { kind: 'cooling'; threshold: number }
  | { kind: 'awaitingPlateClear' }
  | null;

/**
 * Derive the farm phase for a printer:
 * - printing: a job is actively running;
 * - cooling: a cooldown watch is armed — show the release target;
 * - awaitingPlateClear: the gate is raised with NO watch (manual clear needed);
 * - null: nothing farm-phase-worthy to show.
 */
export function deriveFarmPhase(input: FarmPhaseInput): FarmPhase {
  if (input.state === 'RUNNING') return { kind: 'printing' };
  const threshold = input.eject_watch?.threshold_c;
  if (typeof threshold === 'number' && Number.isFinite(threshold)) {
    return { kind: 'cooling', threshold };
  }
  if (input.awaiting_plate_clear) return { kind: 'awaitingPlateClear' };
  return null;
}
