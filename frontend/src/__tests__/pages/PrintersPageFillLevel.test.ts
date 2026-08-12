/**
 * Tests for inventory fill level calculation logic.
 *
 * The fill level is calculated inline in PrintersPage.tsx as:
 *   if (sp && sp.label_weight > 0 && sp.weight_used != null)
 *     → Math.round((remainingFraction(sp) ?? 0) * 100)
 *   else → null
 *
 * The arithmetic itself lives in `utils/spoolGrams` — the mirror below calls
 * the REAL util so the two can never drift; only the page's own
 * "never tracked ⇒ no fill bar" guard is replicated here.
 *
 * These tests validate the calculation logic directly, particularly the
 * fix for weight_used == null (brand new spools) and weight_used == 0, and
 * the spent-aware rule (a spent roll reads 0 % however the ledger looks).
 */
import { describe, it, expect } from 'vitest';
import { remainingFraction } from '../../utils/spoolGrams';

/**
 * Mirrors the inline inventoryFill calculation from PrintersPage.tsx.
 * Extracted here for testability; the derivation is the shared util.
 */
function computeInventoryFill(
  spool: { label_weight: number; weight_used: number | null; spent_at?: string | null } | null,
): number | null {
  const sp = spool;
  if (sp && sp.label_weight > 0 && sp.weight_used != null) {
    return Math.round((remainingFraction(sp) ?? 0) * 100);
  }
  return null;
}

describe('inventoryFill calculation', () => {
  it('returns 100% for brand new spool with weight_used = 0', () => {
    expect(computeInventoryFill({ label_weight: 1000, weight_used: 0 })).toBe(100);
  });

  it('returns null for brand new spool with weight_used = null', () => {
    // weight_used null means "never tracked" — we can't compute fill
    expect(computeInventoryFill({ label_weight: 1000, weight_used: null })).toBeNull();
  });

  it('returns correct percentage for partially used spool', () => {
    expect(computeInventoryFill({ label_weight: 1000, weight_used: 250 })).toBe(75);
  });

  it('returns 0% for fully used spool', () => {
    expect(computeInventoryFill({ label_weight: 1000, weight_used: 1000 })).toBe(0);
  });

  it('returns 0% when weight_used exceeds label_weight', () => {
    // Math.max(0, ...) prevents negative fill
    expect(computeInventoryFill({ label_weight: 1000, weight_used: 1200 })).toBe(0);
  });

  it('returns null when no spool data', () => {
    expect(computeInventoryFill(null)).toBeNull();
  });

  it('returns null when label_weight is 0', () => {
    expect(computeInventoryFill({ label_weight: 0, weight_used: 0 })).toBeNull();
  });

  it('rounds to nearest integer', () => {
    // 1000 - 333 = 667, 667/1000 = 66.7 → 67
    expect(computeInventoryFill({ label_weight: 1000, weight_used: 333 })).toBe(67);
  });

  it('returns 0% for a spent spool even when the ledger says it is full', () => {
    // spent_at is the exhaustion truth — the fill bar must not show a stale
    // positive level for a roll the firmware reported as run out.
    expect(computeInventoryFill({
      label_weight: 1000,
      weight_used: 0,
      spent_at: '2026-08-09T12:00:00Z',
    })).toBe(0);
  });
});

describe('inventoryFill: old bug — weight_used > 0 vs weight_used != null', () => {
  /**
   * The old condition was: sp.weight_used > 0
   * This caused brand-new spools (weight_used=0) to show no fill bar.
   * The fix changed it to: sp.weight_used != null
   */
  it('weight_used = 0 now shows fill (was broken with > 0 check)', () => {
    // Old: 0 > 0 = false → null (no fill bar)
    // New: 0 != null = true → 100% fill
    const result = computeInventoryFill({ label_weight: 1000, weight_used: 0 });
    expect(result).toBe(100);
    expect(result).not.toBeNull();
  });

  it('weight_used = 0.1 shows fill (small usage)', () => {
    const result = computeInventoryFill({ label_weight: 1000, weight_used: 0.1 });
    expect(result).toBe(100); // rounds to 100 since 0.1g from 1000g is negligible
  });
});
