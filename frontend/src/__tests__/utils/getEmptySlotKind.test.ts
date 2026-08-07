/**
 * Tests for getEmptySlotKind — the ONE origin of the bare-slot classification
 * shared by PrintersPage and the SpoolBuddy AMS card.
 *
 * The load-bearing case is 'present': the firmware reports a spool SEATED
 * (state 10 = present/not fed, 11 = present/loaded) but shipped no material
 * identity. That used to fall through to 'reset' and render like an empty
 * slot, which is how 004-H2S kept a ~90 % roll invisible for a day.
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

  it('returns "physical" when firmware confirms no spool (state 9)', () => {
    expect(getEmptySlotKind({ tray_type: null, state: 9 })).toBe('physical');
    expect(getEmptySlotKind({ tray_type: '', state: 9 })).toBe('physical');
  });

  it('returns "present" for a seated-but-unread spool (state 10)', () => {
    expect(getEmptySlotKind({ tray_type: null, state: 10 })).toBe('present');
  });

  it('returns "present" for a seated-but-unread spool (state 11)', () => {
    expect(getEmptySlotKind({ tray_type: '', state: 11 })).toBe('present');
  });

  it('returns "reset" for any other state with no material', () => {
    for (const state of [0, 3, 25, 27]) {
      expect(getEmptySlotKind({ tray_type: null, state })).toBe('reset');
    }
  });

  it('returns "reset" when the state is unknown', () => {
    expect(getEmptySlotKind({ tray_type: null, state: null })).toBe('reset');
    expect(getEmptySlotKind({ tray_type: null })).toBe('reset');
    expect(getEmptySlotKind({})).toBe('reset');
  });

  it('returns "reset" for a missing tray', () => {
    expect(getEmptySlotKind(null)).toBe('reset');
    expect(getEmptySlotKind(undefined)).toBe('reset');
  });
});
