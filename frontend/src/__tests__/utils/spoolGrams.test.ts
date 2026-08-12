/**
 * Tests for the ONE spent-aware remaining-grams derivation.
 *
 * The rule under test: `spent_at` is the exhaustion truth, so a spent roll
 * reads 0 g (and 0 fill) no matter what `label_weight - weight_used` says.
 * Everything else is the pure clamp, which is what every pre-existing
 * hand-rolled copy did.
 */
import { describe, it, expect } from 'vitest';
import { remainingGrams, remainingFraction } from '../../utils/spoolGrams';

describe('remainingGrams', () => {
  it('subtracts used from label for a healthy roll', () => {
    expect(remainingGrams({ label_weight: 1000, weight_used: 250 })).toBe(750);
  });

  it('returns the full label for an untouched roll', () => {
    expect(remainingGrams({ label_weight: 1000, weight_used: 0 })).toBe(1000);
  });

  it('clamps at 0 when the roll is charged past its label', () => {
    // Storage is never floored — a roll CAN read 1114.99 g used on a 1000 g
    // label (the 009-H2S misattribution). Display must not go negative.
    expect(remainingGrams({ label_weight: 1000, weight_used: 1114.99 })).toBe(0);
  });

  it('returns 0 for a spent roll regardless of the ledger', () => {
    expect(remainingGrams({
      label_weight: 1000,
      weight_used: 0,
      spent_at: '2026-08-09T12:00:00Z',
    })).toBe(0);
  });

  it('returns 0 for a spent roll even at full weight', () => {
    // The spent-∧-loaded-full contradiction class: the ledger says 100 %,
    // the firmware said the roll ran out. Spent wins.
    expect(remainingGrams({
      label_weight: 750,
      weight_used: 0,
      spent_at: '2026-08-09T12:00:00Z',
    })).toBe(0);
  });

  it('treats an absent spent_at as not spent', () => {
    expect(remainingGrams({ label_weight: 1000, weight_used: 400 })).toBe(600);
  });

  it('treats a null spent_at as not spent', () => {
    expect(remainingGrams({ label_weight: 1000, weight_used: 400, spent_at: null })).toBe(600);
  });

  it('coalesces a null or undefined label to 0', () => {
    expect(remainingGrams({ label_weight: null, weight_used: 100 })).toBe(0);
    expect(remainingGrams({ weight_used: 100 })).toBe(0);
  });

  it('coalesces a null or undefined used weight to 0', () => {
    expect(remainingGrams({ label_weight: 1000, weight_used: null })).toBe(1000);
    expect(remainingGrams({ label_weight: 1000 })).toBe(1000);
  });

  it('returns 0 for a wholly empty source', () => {
    expect(remainingGrams({})).toBe(0);
  });
});

describe('remainingFraction', () => {
  it('returns the remaining share of the label', () => {
    expect(remainingFraction({ label_weight: 1000, weight_used: 250 })).toBe(0.75);
  });

  it('returns 1 for an untouched roll', () => {
    expect(remainingFraction({ label_weight: 1000, weight_used: 0 })).toBe(1);
  });

  it('returns 0 for an over-used roll', () => {
    expect(remainingFraction({ label_weight: 1000, weight_used: 1500 })).toBe(0);
  });

  it('returns 0 for a spent roll', () => {
    expect(remainingFraction({
      label_weight: 1000,
      weight_used: 0,
      spent_at: '2026-08-09T12:00:00Z',
    })).toBe(0);
  });

  it('returns null when the label weight is 0 — no denominator', () => {
    expect(remainingFraction({ label_weight: 0, weight_used: 0 })).toBeNull();
  });

  it('returns null when the label weight is negative', () => {
    expect(remainingFraction({ label_weight: -5, weight_used: 0 })).toBeNull();
  });

  it('returns null when the label weight is missing or null', () => {
    expect(remainingFraction({ weight_used: 100 })).toBeNull();
    expect(remainingFraction({ label_weight: null, weight_used: 100 })).toBeNull();
  });

  it('is null rather than 0 for an unpriceable spent roll', () => {
    // Spent still cannot invent a denominator — callers distinguish
    // "empty" (0) from "unknowable" (null).
    expect(remainingFraction({ label_weight: 0, spent_at: '2026-08-09T12:00:00Z' })).toBeNull();
  });
});
