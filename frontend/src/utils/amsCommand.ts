import type { AmsCommandOutcome } from '../api/client';
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
  | 'printers.toast.amsUnloadHeld';

export interface AmsCommandToast {
  key: AmsCommandToastKey;
  type: ToastType;
}

/**
 * The toast an operator AMS load/unload renders for what the wire answered — the
 * ONE mapping both PrintersPage mutations share.
 *
 * Keyed off `outcome`, never off the response `message`: that sentence is the
 * backend's English fallback for non-UI clients and is not rendered.
 *
 * `undecidable` is only produced for an unload (the backend's classifier has no
 * load row that answers it); a load answered `undecidable` still maps to the
 * nothing-loaded copy so the table has no hole.
 *
 * `held` is a warning, not a success: the AMS accepted the command but runs it only
 * after the paused print's own filament change releases.
 *
 * The `never` default makes a new backend outcome a `tsc -b` failure here rather
 * than a silent fall-through; at runtime an unknown outcome throws, which TanStack
 * Query routes to the mutation's `onError`.
 */
export function amsCommandToast(command: AmsCommand, outcome: AmsCommandOutcome): AmsCommandToast {
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
      return {
        key: command === 'load' ? 'printers.toast.amsLoadHeld' : 'printers.toast.amsUnloadHeld',
        type: 'warning',
      };
    default: {
      const unhandled: never = outcome;
      throw new Error(`Unhandled AMS command outcome: ${String(unhandled)}`);
    }
  }
}
