import type { AmsCommandOutcome, AmsPostureFamily } from '../api/client';
import type { ToastType } from '../contexts/ToastContext';

/** The two operator AMS motion verbs whose 200 body carries an `AmsCommandOutcome`. */
export type AmsCommand = 'load' | 'unload';

/** The i18n keys an AMS command outcome can render — the whole copy surface of the mapping. */
export type AmsCommandToastKey =
  | 'printers.toast.loadInitiated'
  | 'printers.toast.unloadInitiated'
  | 'printers.toast.amsLoadNoMovement'
  | 'printers.toast.amsUnloadNoMovement'
  | 'printers.toast.amsUnloadNothingLoaded'
  | 'printers.toast.amsCommandSessionChanged'
  | 'printers.toast.amsLoadHeld'
  | 'printers.toast.amsUnloadHeld'
  | 'printers.toast.amsLoadHeldOutsideChange'
  | 'printers.toast.amsUnloadHeldOutsideChange';

export interface AmsCommandToast {
  key: AmsCommandToastKey;
  type: ToastType;
}

/**
 * `held` copy per posture family. Inside the paused print's own filament change the
 * command waits behind that change; outside one it is accepted and not run yet, and
 * the AMS can run it later on its own (011-H2S / 014-H2S 2026-10-09/10: a pull-back
 * ran ~4.5 min after its ACK with the print still PAUSED).
 *
 * A `null` family (the backend sends one on every 200, so only an older backend) takes
 * the outside-change copy: "not run yet" is true in every posture, while "held behind
 * the paused print's filament change" claims a posture nobody reported.
 */
function heldKey(command: AmsCommand, family: AmsPostureFamily | null): AmsCommandToastKey {
  if (family === 'mid_change') {
    return command === 'load' ? 'printers.toast.amsLoadHeld' : 'printers.toast.amsUnloadHeld';
  }
  return command === 'load'
    ? 'printers.toast.amsLoadHeldOutsideChange'
    : 'printers.toast.amsUnloadHeldOutsideChange';
}

/**
 * The toast an operator AMS load/unload renders for what the wire answered — the
 * ONE mapping both PrintersPage mutations share.
 *
 * Keyed off `outcome` and, where the copy differs by posture, the posture `family`
 * the command was sent into — never off the response `message`: that sentence is
 * the backend's English fallback for non-UI clients and is not rendered.
 *
 * `undecidable` is only produced for an unload (the backend's classifier has no
 * load row that answers it); a load answered `undecidable` still maps to the
 * nothing-loaded copy so the table has no hole.
 *
 * `held` is a warning, not a success: the AMS accepted the command but has not run it.
 *
 * The `never` default makes a new backend outcome a `tsc -b` failure here rather
 * than a silent fall-through; at runtime an unknown outcome throws, which TanStack
 * Query routes to the mutation's `onError`.
 */
export function amsCommandToast(
  command: AmsCommand,
  outcome: AmsCommandOutcome,
  family: AmsPostureFamily | null,
): AmsCommandToast {
  switch (outcome) {
    case 'complete':
    case 'acted':
      return {
        key: command === 'load' ? 'printers.toast.loadInitiated' : 'printers.toast.unloadInitiated',
        type: 'success',
      };
    case 'no_movement':
      return {
        key: command === 'load' ? 'printers.toast.amsLoadNoMovement' : 'printers.toast.amsUnloadNoMovement',
        type: 'warning',
      };
    case 'undecidable':
      return { key: 'printers.toast.amsUnloadNothingLoaded', type: 'info' };
    case 'session_changed':
      return { key: 'printers.toast.amsCommandSessionChanged', type: 'warning' };
    case 'held':
      return { key: heldKey(command, family), type: 'warning' };
    default: {
      const unhandled: never = outcome;
      throw new Error(`Unhandled AMS command outcome: ${String(unhandled)}`);
    }
  }
}
