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
 *
 * A DEFERRED eject is its own phase because nothing else can name it:
 * `service_hold` + `eject_watch` alone cannot tell "cooling under a hold" from
 * "cooled, fans retired, waiting on the hold" — both carry a hold and an armed
 * watch with a threshold. Only the watch itself knows which side of the
 * cooldown it is on, so it reports `eject_watch.deferred`.
 */

export interface FarmPhaseInput {
  /** Raw printer gcode_state (RUNNING / PAUSE / FINISH / FAILED / IDLE…). */
  state?: string | null;
  awaiting_plate_clear?: boolean;
  eject_watch?: { threshold_c: number; hold_z?: number | null; deferred?: boolean } | null;
}

export type FarmPhase =
  | { kind: 'printing' }
  | { kind: 'cooling'; threshold: number; held: boolean }
  | { kind: 'ejectDeferred'; held: boolean }
  | { kind: 'awaitingPlateClear' }
  | null;

/**
 * Derive the farm phase for a printer:
 * - printing: a job is actively running;
 * - ejectDeferred: the cooldown episode is over (bed at threshold, or a bed at
 *   equilibrium), the fans are retired, and the eject is withheld until
 *   maintenance mode ends — so it is NOT necessarily a cold plate;
 * - cooling: a cooldown watch is armed — show the release target;
 * - awaitingPlateClear: the gate is raised with NO watch (manual clear needed);
 * - null: nothing farm-phase-worthy to show.
 *
 * `held` is true on BOTH plate-bearing phases when the watch reports a `hold_z`:
 * the farm is holding the plate raised at the nozzle plane with the toolhead
 * parked at the chute, so the toolhead must not be jogged until the eject runs.
 * One derivation serves both — a deferred eject can sit on a raised plate, and
 * the backend keeps that claim while the hold stands.
 */
export function deriveFarmPhase(input: FarmPhaseInput): FarmPhase {
  if (input.state === 'RUNNING') return { kind: 'printing' };
  const holdZ = input.eject_watch?.hold_z;
  const held = typeof holdZ === 'number' && Number.isFinite(holdZ);
  // A deferred eject outranks cooling: the watch still reports its threshold,
  // but the cooldown is over and only the hold stands between plate and sweep.
  if (input.eject_watch?.deferred === true) return { kind: 'ejectDeferred', held };
  const threshold = input.eject_watch?.threshold_c;
  if (typeof threshold === 'number' && Number.isFinite(threshold)) {
    return { kind: 'cooling', threshold, held };
  }
  if (input.awaiting_plate_clear) return { kind: 'awaitingPlateClear' };
  return null;
}
