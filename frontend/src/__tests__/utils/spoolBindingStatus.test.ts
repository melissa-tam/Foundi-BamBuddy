/**
 * The one binding-status derivation shared by the printer card's
 * EmptySlotHoverCard and the Inventory LOCATION column (W5a/W5b).
 *
 * Two rules are worth pinning. Precedence: `spent` outranks `preConfigured`,
 * because `pre_configured_at` is a bind-time stamp that an insert-then-run-dry
 * cycle never clears. Presence honesty: "not inserted" is a claim about the
 * physical world and may only be made when presence is actually FALSE — the
 * card used to state it under a header reading "Spool present — unrecognized".
 */

import { describe, it, expect } from 'vitest';
import { resolveSpoolBindingStatus, slotPresence } from '../../utils/spoolBindingStatus';

describe('slotPresence', () => {
  it('reads the live wire kind first', () => {
    expect(slotPresence({ kind: 'present' })).toBe('seated');
    expect(slotPresence({ kind: 'physical' })).toBe('empty');
  });

  it('lets the wire kind win over a disagreeing API tri-state', () => {
    // Same status push, but the tray in this render is the fresher evidence.
    expect(slotPresence({ kind: 'present', present: false })).toBe('seated');
    expect(slotPresence({ kind: 'physical', present: true })).toBe('empty');
  });

  it('defers to the API tri-state when the wire kind is unknown or absent', () => {
    // Evidence is only ever ADDED: an unknown kind must not veto an answer the
    // API has (it also sees the unit-level exist bits).
    expect(slotPresence({ kind: 'unknown', present: true })).toBe('seated');
    expect(slotPresence({ kind: 'unknown', present: false })).toBe('empty');
    expect(slotPresence({ present: true })).toBe('seated');
    expect(slotPresence({ present: false })).toBe('empty');
  });

  it('answers unknown when nothing knows', () => {
    expect(slotPresence({})).toBe('unknown');
    expect(slotPresence({ kind: 'unknown' })).toBe('unknown');
    expect(slotPresence({ kind: null, present: null })).toBe('unknown');
  });
});

describe('resolveSpoolBindingStatus', () => {
  it('reports a stale claim only when the slot is verifiably empty', () => {
    const s = resolveSpoolBindingStatus({ spent: false, preConfigured: false, presence: 'empty' });
    expect(s.id).toBe('notInserted');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.notInserted');
  });

  it('reports seated-awaiting-identification on a seated but unread slot', () => {
    // The contradiction this closes: header "Spool present — unrecognized" over
    // a binding line reading "not inserted".
    const s = resolveSpoolBindingStatus({ spent: false, preConfigured: false, presence: 'seated' });
    expect(s.id).toBe('seatedUnread');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.seatedUnread');
  });

  it('never asserts "not inserted" when presence is unknown', () => {
    for (const binding of [
      { spent: false, preConfigured: false, presence: 'unknown' as const },
      { spent: false, preConfigured: false },  // caller supplied no presence at all
    ]) {
      const s = resolveSpoolBindingStatus(binding);
      expect(s.id).toBe('presenceUnknown');
      expect(s.i18nKey).toBe('ams.emptySlotBinding.presenceUnknown');
    }
  });

  it('reports awaiting-insert for a deliberate bind-to-empty', () => {
    const s = resolveSpoolBindingStatus({ spent: false, preConfigured: true, presence: 'empty' });
    expect(s.id).toBe('awaitingInsert');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.awaitingInsert');
  });

  it('reports ran-out for a spent roll', () => {
    const s = resolveSpoolBindingStatus({ spent: true, preConfigured: false, presence: 'empty' });
    expect(s.id).toBe('ranOut');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.ranOut');
  });

  it('lets spent outrank pre-configured when both are set', () => {
    const s = resolveSpoolBindingStatus({ spent: true, preConfigured: true, presence: 'empty' });
    expect(s.id).toBe('ranOut');
  });

  it('lets the latches outrank presence — a spent roll reads "ran out" wherever it sits', () => {
    expect(resolveSpoolBindingStatus({ spent: true, preConfigured: false, presence: 'seated' }).id)
      .toBe('ranOut');
    expect(resolveSpoolBindingStatus({ spent: false, preConfigured: true, presence: 'seated' }).id)
      .toBe('awaitingInsert');
  });

  it('pairs every state with a distinct icon so colour is never the only cue', () => {
    const states = [
      resolveSpoolBindingStatus({ spent: false, preConfigured: false, presence: 'empty' }),
      resolveSpoolBindingStatus({ spent: false, preConfigured: false, presence: 'seated' }),
      resolveSpoolBindingStatus({ spent: false, preConfigured: false, presence: 'unknown' }),
      resolveSpoolBindingStatus({ spent: false, preConfigured: true, presence: 'empty' }),
      resolveSpoolBindingStatus({ spent: true, preConfigured: false, presence: 'empty' }),
    ];
    const icons = new Set(states.map(s => s.Icon));
    expect(icons.size).toBe(5);
    for (const s of states) {
      expect(s.className).toBeTruthy();
      expect(s.Icon).toBeTruthy();
    }
  });
});
