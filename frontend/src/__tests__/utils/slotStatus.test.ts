/**
 * Pure derivation behind the PrintersPage slot badges: the per-slot ran-out
 * flag. (The slot rings are the backend's answer; see
 * `__tests__/pages/PrintersPage.feedRing.test.tsx`.)
 */
import { describe, it, expect } from 'vitest';
import { slotRanOut, type RunoutSlotBearer } from '../../utils/slotStatus';

describe('slotRanOut', () => {
  const errors: RunoutSlotBearer[] = [
    { runout_slot: { ams_id: 0, tray_id: 2 } },
    { runout_slot: null },
    {},
  ];

  it('flags the slot named by a runout HMS error', () => {
    expect(slotRanOut(errors, 0, 2)).toBe(true);
  });

  it('does not flag other slots on the same or other AMS units', () => {
    expect(slotRanOut(errors, 0, 1)).toBe(false);
    expect(slotRanOut(errors, 1, 2)).toBe(false);
  });

  it('is false when no error carries a runout_slot', () => {
    expect(slotRanOut([{ runout_slot: null }, {}], 0, 2)).toBe(false);
  });

  it('is false for empty / nullish error lists', () => {
    expect(slotRanOut([], 0, 2)).toBe(false);
    expect(slotRanOut(null, 0, 2)).toBe(false);
    expect(slotRanOut(undefined, 0, 2)).toBe(false);
  });
});
