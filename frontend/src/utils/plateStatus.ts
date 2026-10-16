/**
 * THE plate pill: what the printer card and the touch tile say about the build plate,
 * its tone, and the supplementary constraint that rides its tooltip. One origin, so the
 * two surfaces can never word the same plate differently.
 *
 * A raised gate always surfaces (it blocks dispatch whatever the toggle says); the
 * informational In use / Cleared pills only when `require_plate_clear` is on, so a
 * pure-upstream card is not cluttered. During the farm's cooldown the hold is EXPECTED,
 * so the pill quotes the cooling phase instead of the alarming "not cleared".
 */
import type { PrinterStatus } from '../api/client';
import { coolingLabel, deriveFarmPhase } from './farmPhase';

export interface PlateStatusView {
  /** The pill's one-line label: an i18n key and its interpolation values. */
  label: { key: string; values?: { threshold: number | null } };
  /** Tailwind classes for the pill's tone. */
  className: string;
  /** Supplementary constraint (tooltip only, never inline); null when none. */
  hintKey: string | null;
}

const IN_USE = 'bg-blue-500/20 text-blue-400';

/** The plate pill for this status, or null when the plate has nothing to report. */
export function plateStatusOf(
  status: PrinterStatus | undefined,
  requirePlateClear: boolean,
): PlateStatusView | null {
  if (!status?.connected) return null;
  if (!requirePlateClear && !status.awaiting_plate_clear) return null;
  if (status.state === 'RUNNING' || status.state === 'PAUSE') {
    return { label: { key: 'printers.plateStatus.inUse' }, className: IN_USE, hintKey: null };
  }
  if (status.awaiting_plate_clear) {
    const phase = deriveFarmPhase({
      state: status.state,
      awaiting_plate_clear: status.awaiting_plate_clear,
      eject_watch: status.eject_watch,
    });
    if (phase?.kind === 'ejectDeferred') {
      // Cooldown over, fans retired, sweep withheld by maintenance mode. A plate still
      // held at the nozzle plane carries the do-not-jog constraint as well.
      return phase.held
        ? {
            label: { key: 'printers.phase.ejectDeferredHeld' },
            className: IN_USE,
            hintKey: 'printers.phase.ejectDeferredHeldHint',
          }
        : {
            label: { key: 'printers.phase.ejectDeferred' },
            className: IN_USE,
            hintKey: 'printers.phase.ejectDeferredHint',
          };
    }
    if (phase?.kind === 'cooling') {
      const cooling = coolingLabel(phase);
      return {
        label: { key: cooling.key, values: { threshold: cooling.threshold } },
        className: IN_USE,
        hintKey: phase.held ? 'printers.phase.coolingHeldHint' : null,
      };
    }
    return {
      label: { key: 'printers.plateStatus.notCleared' },
      className: 'bg-yellow-500/20 text-yellow-400',
      hintKey: null,
    };
  }
  return {
    label: { key: 'printers.plateStatus.cleared' },
    className: 'bg-status-ok/20 text-status-ok',
    hintKey: null,
  };
}
