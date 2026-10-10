/**
 * Tests for amsCommandToast — the ONE outcome → toast mapping behind the
 * PrintersPage AMS Load/Unload mutations.
 *
 * The contract this pins: the copy is keyed off the wire `outcome` and, for
 * `held`, the posture `family` the command was sent into (never the backend's
 * English `message`), every outcome × command × family cell has a row, and every
 * key the mapping can emit resolves in the `en` locale.
 */
import { describe, it, expect } from 'vitest';
import type { AmsCommandOutcome, AmsPostureFamily } from '../../api/client';
import type { ToastType } from '../../contexts/ToastContext';
import { amsCommandToast, type AmsCommand, type AmsCommandToastKey } from '../../utils/amsCommand';
import en from '../../i18n/locales/en';

type Row = [AmsCommand, AmsCommandOutcome, AmsPostureFamily, AmsCommandToastKey, ToastType];

/** Outcomes whose copy is the same in both posture families. */
const familyBlind: Array<[AmsCommand, AmsCommandOutcome, AmsCommandToastKey, ToastType]> = [
  ['load', 'complete', 'printers.toast.loadInitiated', 'success'],
  ['load', 'acted', 'printers.toast.loadInitiated', 'success'],
  ['load', 'no_movement', 'printers.toast.amsLoadNoMovement', 'warning'],
  ['load', 'undecidable', 'printers.toast.amsUnloadNothingLoaded', 'info'],
  ['load', 'session_changed', 'printers.toast.amsCommandSessionChanged', 'warning'],
  ['unload', 'complete', 'printers.toast.unloadInitiated', 'success'],
  ['unload', 'acted', 'printers.toast.unloadInitiated', 'success'],
  ['unload', 'no_movement', 'printers.toast.amsUnloadNoMovement', 'warning'],
  ['unload', 'undecidable', 'printers.toast.amsUnloadNothingLoaded', 'info'],
  ['unload', 'session_changed', 'printers.toast.amsCommandSessionChanged', 'warning'],
];

const families: AmsPostureFamily[] = ['mid_change', 'outside_change'];

const table: Row[] = [
  ...familyBlind.flatMap(([command, outcome, key, type]) =>
    families.map((family): Row => [command, outcome, family, key, type]),
  ),
  // `held` is the one outcome whose copy follows the posture.
  ['load', 'held', 'mid_change', 'printers.toast.amsLoadHeld', 'warning'],
  ['unload', 'held', 'mid_change', 'printers.toast.amsUnloadHeld', 'warning'],
  ['load', 'held', 'outside_change', 'printers.toast.amsLoadHeldOutsideChange', 'warning'],
  ['unload', 'held', 'outside_change', 'printers.toast.amsUnloadHeldOutsideChange', 'warning'],
];

/** Walks a dotted i18n key through the `en` locale object. */
function enValue(key: string): unknown {
  return key.split('.').reduce<unknown>(
    (node, part) => (node !== null && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
    en,
  );
}

describe('amsCommandToast', () => {
  it.each(table)('%s answered %s (%s) → %s (%s)', (command, outcome, family, key, type) => {
    expect(amsCommandToast(command, outcome, family)).toEqual({ key, type });
  });

  it.each(table)('%s answered %s (%s) emits a key present in en', (command, outcome, family) => {
    const { key } = amsCommandToast(command, outcome, family);
    const value = enValue(key);
    expect(typeof value).toBe('string');
    expect(value).not.toBe('');
  });

  it('reads an unreported posture as "not run yet", never as held behind a change', () => {
    expect(amsCommandToast('load', 'held', null).key).toBe('printers.toast.amsLoadHeldOutsideChange');
    expect(amsCommandToast('unload', 'held', null).key).toBe('printers.toast.amsUnloadHeldOutsideChange');
  });

  it('throws on an outcome outside the contract instead of rendering nothing', () => {
    expect(() =>
      amsCommandToast('unload', 'refused_runout_hold' as unknown as AmsCommandOutcome, 'mid_change'),
    ).toThrow('Unhandled AMS command outcome: refused_runout_hold');
  });
});
