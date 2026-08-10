/**
 * Tests for getEmptySlotKind — the ONE origin of the bare-slot classification
 * shared by PrintersPage and the SpoolBuddy AMS card.
 *
 * Two load-bearing cases, both of them states the UI used to draw as "empty":
 *
 * 'present' — the firmware reports a spool SEATED (state 10 = present/not fed,
 * 11 = present/loaded) but shipped no material identity. That used to fall
 * through to the catch-all kind and render like an empty slot, which is how
 * 004-H2S kept a ~90 % roll invisible for a day.
 *
 * 'unknown' — nothing on the wire said either way. The classifier mirrors the
 * backend `tray_presence` tri-state, which answers "no spool" ONLY for the
 * asserted cleared shape (state 9 AND tray_type === ""). A bare state 9 with no
 * material key, and every dialect idle state, are the absence of an answer.
 */

import { describe, it, expect } from 'vitest';

import { getEmptySlotKind } from '../../utils/amsHelpers';

describe('getEmptySlotKind', () => {
  it('returns null for a loaded slot (tray_type present)', () => {
    expect(getEmptySlotKind({ tray_type: 'PETG', state: 11 })).toBeNull();
  });

  it('returns null for a loaded slot regardless of state', () => {
    // A configured slot is never an "empty kind", even on a present state.
    expect(getEmptySlotKind({ tray_type: 'PLA', state: 10 })).toBeNull();
    expect(getEmptySlotKind({ tray_type: 'PLA', state: 9 })).toBeNull();
    expect(getEmptySlotKind({ tray_type: 'PLA', state: null })).toBeNull();
  });

  it('returns "physical" only for the ASSERTED cleared shape (state 9 + tray_type "")', () => {
    expect(getEmptySlotKind({ tray_type: '', state: 9 })).toBe('physical');
  });

  it('returns "unknown" for a state-9 tray that never asserted an empty material', () => {
    // The regression this closes: a key-less minimal tray (`{id, state: 9}`) is
    // exactly what a boot-forgotten slot sends, and claiming "empty" for it is a
    // fact the wire never stated. null and undefined are both non-assertions.
    expect(getEmptySlotKind({ tray_type: null, state: 9 })).toBe('unknown');
    expect(getEmptySlotKind({ state: 9 })).toBe('unknown');
  });

  it('returns "present" for a seated-but-unread spool (state 10)', () => {
    expect(getEmptySlotKind({ tray_type: null, state: 10 })).toBe('present');
  });

  it('returns "present" for a seated-but-unread spool (state 11)', () => {
    expect(getEmptySlotKind({ tray_type: '', state: 11 })).toBe('present');
  });

  it('returns "unknown" for the dialect states, which say nothing about presence', () => {
    // A1 / P1S / H2C idle slots sit in these; the old "reset" kind rendered them
    // as a user-cleared slot, an event that never happened on those printers.
    for (const state of [0, 3, 8, 25, 26, 27]) {
      expect(getEmptySlotKind({ tray_type: null, state })).toBe('unknown');
    }
  });

  it('returns "unknown" when the state is missing', () => {
    expect(getEmptySlotKind({ tray_type: null, state: null })).toBe('unknown');
    expect(getEmptySlotKind({ tray_type: null })).toBe('unknown');
    expect(getEmptySlotKind({})).toBe('unknown');
  });

  it('returns "unknown" for a missing tray', () => {
    expect(getEmptySlotKind(null)).toBe('unknown');
    expect(getEmptySlotKind(undefined)).toBe('unknown');
  });
});
