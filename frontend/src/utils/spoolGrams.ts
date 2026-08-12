/**
 * The ONE frontend derivation of a spool's remaining filament.
 *
 * Remaining grams are **spent-aware**: `spent_at` is the hardware-certain
 * exhaustion truth, so a spent roll reads 0 g regardless of what the ledger
 * arithmetic says. Only when the roll is not spent does remaining fall back to
 * `max(0, label_weight - weight_used)`.
 *
 * This mirrors the backend `Spool.remaining_g` property. Storage is never
 * floored — `weight_used` may legitimately exceed `label_weight` (a roll charged
 * past its label) and `spent_at` is an independent column. Zero is DERIVED here
 * at read time, never written back.
 *
 * Every "how much is left on this roll?" site in the frontend must call these
 * helpers rather than hand-rolling the subtraction, so that a spent roll can
 * never render a stale positive weight on one surface while reading 0 on
 * another.
 */

/**
 * The minimal shape the derivation needs. All three fields are optional so the
 * helpers accept Spoolman-shaped objects, in-progress form data and partial
 * fixtures alike; an absent `spent_at` simply means "not spent", which degrades
 * the derivation to the pure clamp.
 */
export interface SpoolGramsSource {
  label_weight?: number | null;
  weight_used?: number | null;
  /** Absent/undefined ⇒ not spent (form data, Spoolman objects). */
  spent_at?: string | null;
}

/**
 * Grams of filament left on the roll.
 *
 * @returns 0 when the roll is spent; otherwise `max(0, label - used)` with
 *          null/undefined weights coalescing to 0.
 */
export function remainingGrams(spool: SpoolGramsSource): number {
  if (spool.spent_at) return 0;
  const label = spool.label_weight ?? 0;
  const used = spool.weight_used ?? 0;
  return Math.max(0, label - used);
}

/**
 * Remaining filament as a 0..1 fraction of the label weight — the fill level.
 *
 * @returns null when the label weight is missing or ≤ 0 (no denominator, so
 *          fill is genuinely unknowable); otherwise `remainingGrams / label`,
 *          which is 0 for a spent roll.
 */
export function remainingFraction(spool: SpoolGramsSource): number | null {
  const label = spool.label_weight ?? 0;
  if (label <= 0) return null;
  return remainingGrams(spool) / label;
}
