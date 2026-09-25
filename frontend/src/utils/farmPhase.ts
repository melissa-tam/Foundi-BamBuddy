/**
 * Farm cooldown/eject phase derivation (Phase 4.3c).
 *
 * The loop between two units looks idle without this: the printer sits in
 * FINISH with the plate gate raised while the server-side cooldown watch waits
 * for the bed to cool. `eject_watch` (from printer status) is present exactly
 * while a watch is COOLING, and carries the eject line it armed with — or null
 * when shop air was unknown at arm, in which case the bed still cools and
 * releases on its own air or at its plateau; there is just no line to quote.
 * One pure function shared by the printer card and the run detail page —
 * derived on render, never stored.
 *
 * The physical sweep itself is not separately observable — the server
 * dispatches the eject job once the bed reaches the release point, so it is
 * deliberately folded into the cooling phase rather than shown as its own.
 *
 * A DEFERRED eject is its own phase because nothing else can name it:
 * `service_hold` + `eject_watch` alone cannot tell "cooling under a hold" from
 * "cooled, fans retired, waiting on the hold" — both carry a hold and an armed
 * watch. Only the watch itself knows which side of the cooldown it is on, so it
 * reports `eject_watch.deferred`.
 */
import type { EjectWatchInfo } from '../api/client';

export interface FarmPhaseInput {
  /** Raw printer gcode_state (RUNNING / PAUSE / FINISH / FAILED / IDLE…). */
  state?: string | null;
  awaiting_plate_clear?: boolean;
  /** Older status payloads omit `hold_z` / `deferred`; only the line is always sent. */
  eject_watch?:
    | (Pick<EjectWatchInfo, 'threshold_c'> & Partial<Pick<EjectWatchInfo, 'hold_z' | 'deferred'>>)
    | null;
}

export type FarmPhase =
  | { kind: 'printing' }
  /** `threshold` is the eject line; null = no measured line (cooling to plateau). */
  | { kind: 'cooling'; threshold: number | null; held: boolean }
  | { kind: 'ejectDeferred'; held: boolean }
  | { kind: 'awaitingPlateClear' }
  | null;

export type CoolingPhase = Extract<FarmPhase, { kind: 'cooling' }>;

/**
 * Derive the farm phase for a printer:
 * - printing: a job is actively running;
 * - ejectDeferred: the cooldown episode is over (bed at the line, or a bed at
 *   equilibrium), the fans are retired, and the eject is withheld until
 *   maintenance mode ends — so it is NOT necessarily a cold plate;
 * - cooling: a cooldown watch is armed — show the eject line, or the plateau
 *   when the watch armed without one;
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
  const watch = input.eject_watch;
  if (watch) {
    const holdZ = watch.hold_z;
    const held = typeof holdZ === 'number' && Number.isFinite(holdZ);
    // A deferred eject outranks cooling: the watch still reports its line, but
    // the cooldown is over and only the hold stands between plate and sweep.
    if (watch.deferred === true) return { kind: 'ejectDeferred', held };
    // The watch's presence is the cooling fact; the line is only what it quotes.
    // A non-finite line is no line — never a number shown to the operator.
    const line = watch.threshold_c;
    const threshold = typeof line === 'number' && Number.isFinite(line) ? line : null;
    return { kind: 'cooling', threshold, held };
  }
  if (input.awaiting_plate_clear) return { kind: 'awaitingPlateClear' };
  return null;
}

export type CoolingLabelKey =
  | 'printers.phase.cooling'
  | 'printers.phase.coolingHeld'
  | 'printers.phase.coolingToPlateau'
  | 'printers.phase.coolingToPlateauHeld';

export interface CoolingLabel {
  key: CoolingLabelKey;
  /** The eject line rounded for display; null for the plateau keys (no placeholder). */
  threshold: number | null;
}

/**
 * THE choice of the cooling label: eject line or plateau, held or not. Both
 * surfaces that name a cooling printer (the printer card pill and the run
 * detail chip) render `t(label.key, { threshold: label.threshold })`, so the
 * two can never word the same watch differently.
 */
export function coolingLabel(phase: CoolingPhase): CoolingLabel {
  if (phase.threshold === null) {
    return {
      key: phase.held ? 'printers.phase.coolingToPlateauHeld' : 'printers.phase.coolingToPlateau',
      threshold: null,
    };
  }
  return {
    key: phase.held ? 'printers.phase.coolingHeld' : 'printers.phase.cooling',
    threshold: Math.round(phase.threshold),
  };
}
