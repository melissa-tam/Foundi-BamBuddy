/**
 * Pure derivations behind the PrintersPage W6 slot badges/ring: the was-feeding
 * dimmed-ring tray id and the per-slot ran-out flag.
 */
import { describe, it, expect } from 'vitest';
import { wasFeedingTrayId, slotRanOut, type RunoutSlotBearer } from '../../utils/slotStatus';

describe('wasFeedingTrayId', () => {
  it('returns the last-loaded tray during a runout PAUSE (no live active tray)', () => {
    // PAUSE, tray_now cleared (effectiveTrayNow undefined), last_loaded_tray = 3.
    expect(wasFeedingTrayId('PAUSE', undefined, 3)).toBe(3);
    expect(wasFeedingTrayId('RUNNING', undefined, 0)).toBe(0);
  });

  it('returns undefined when a live active tray is present (ring not needed)', () => {
    // effectiveTrayNow is defined → the real green ring shows; no was-feeding hint.
    expect(wasFeedingTrayId('PAUSE', 3, 3)).toBeUndefined();
    expect(wasFeedingTrayId('RUNNING', 1, 3)).toBeUndefined();
  });

  it('returns undefined when no job is active', () => {
    expect(wasFeedingTrayId('IDLE', undefined, 3)).toBeUndefined();
    expect(wasFeedingTrayId('FINISH', undefined, 3)).toBeUndefined();
    expect(wasFeedingTrayId(null, undefined, 3)).toBeUndefined();
    expect(wasFeedingTrayId(undefined, undefined, 3)).toBeUndefined();
  });

  it('returns undefined when last_loaded_tray names no tray (-1 / null)', () => {
    expect(wasFeedingTrayId('PAUSE', undefined, -1)).toBeUndefined();
    expect(wasFeedingTrayId('PAUSE', undefined, null)).toBeUndefined();
    expect(wasFeedingTrayId('PAUSE', undefined, undefined)).toBeUndefined();
  });

  it('treats tray 0 as a real tray (not falsy)', () => {
    expect(wasFeedingTrayId('PAUSE', undefined, 0)).toBe(0);
  });
});

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
