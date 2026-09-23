/**
 * Tests for amsCommandToast — the ONE outcome → toast mapping behind the
 * PrintersPage AMS Load/Unload mutations.
 *
 * The contract this pins: the copy is keyed off the wire `outcome` (never the
 * backend's English `message`), every outcome × command pair has a row, and every
 * key the mapping can emit resolves in the `en` locale.
 */
import { describe, it, expect } from 'vitest';
import type { AmsCommandOutcome } from '../../api/client';
import type { ToastType } from '../../contexts/ToastContext';
import { amsCommandToast, type AmsCommand, type AmsCommandToastKey } from '../../utils/amsCommand';
import en from '../../i18n/locales/en';

const table: Array<[AmsCommand, AmsCommandOutcome, AmsCommandToastKey, ToastType]> = [
  ['load', 'complete', 'printers.toast.loadInitiated', 'success'],
  ['load', 'acted', 'printers.toast.loadInitiated', 'success'],
  ['load', 'no_movement', 'printers.toast.amsLoadNoMovement', 'warning'],
  ['load', 'undecidable', 'printers.toast.amsUnloadNothingLoaded', 'info'],
  ['load', 'session_changed', 'printers.toast.amsCommandSessionChanged', 'warning'],
  ['load', 'held', 'printers.toast.amsLoadHeld', 'warning'],
  ['unload', 'complete', 'printers.toast.unloadInitiated', 'success'],
  ['unload', 'acted', 'printers.toast.unloadInitiated', 'success'],
  ['unload', 'no_movement', 'printers.toast.amsUnloadNoMovement', 'warning'],
  ['unload', 'undecidable', 'printers.toast.amsUnloadNothingLoaded', 'info'],
  ['unload', 'session_changed', 'printers.toast.amsCommandSessionChanged', 'warning'],
  ['unload', 'held', 'printers.toast.amsUnloadHeld', 'warning'],
];

/** Walks a dotted i18n key through the `en` locale object. */
function enValue(key: string): unknown {
  return key.split('.').reduce<unknown>(
    (node, part) => (node !== null && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
    en,
  );
}

describe('amsCommandToast', () => {
  it.each(table)('%s answered %s → %s (%s)', (command, outcome, key, type) => {
    expect(amsCommandToast(command, outcome)).toEqual({ key, type });
  });

  it.each(table)('%s answered %s emits a key present in en', (command, outcome) => {
    const { key } = amsCommandToast(command, outcome);
    const value = enValue(key);
    expect(typeof value).toBe('string');
    expect(value).not.toBe('');
  });

  it('throws on an outcome outside the contract instead of rendering nothing', () => {
    expect(() => amsCommandToast('unload', 'refused_runout_hold' as unknown as AmsCommandOutcome)).toThrow(
      'Unhandled AMS command outcome: refused_runout_hold',
    );
  });
});
