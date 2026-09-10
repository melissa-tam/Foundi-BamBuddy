/**
 * The ONE list rule behind the "Assign spool" picker: which rolls are worth
 * offering for a slot, in what order, and which one row may claim "this is the
 * roll that was last here".
 *
 * Filter, order, hidden count and recency verdict live together because they
 * are one decision — a row hidden by the emptiness gate must not be counted in
 * the footer twice, and a row that ranks by residue must be the same row that
 * carries the residue label. The two branches of `AssignSpoolModal` (local
 * inventory and Spoolman) call this once each, so they cannot drift.
 *
 * ## Emptiness gate (skipped entirely when `showAll`)
 * A row is EMPTY, and hidden, iff:
 *   - `spent_at` is set (hardware-certain exhaustion), OR
 *   - `label_weight > 0` AND `remainingGrams(row) <= 0`.
 * A row with no label weight (or zero) has UNKNOWN remaining and stays listed.
 * That mirrors the backend start floor's "unknown remaining = eligible" rule:
 * a missing denominator is not evidence of an empty roll. Remaining grams come
 * from `utils/spoolGrams.remainingGrams` — the ONE frontend origin — never from
 * a re-derived `label − used`.
 *
 * ## Order (stable, computed on a COPY of the input array)
 * | tier | rule                                                        | sorted by            |
 * |------|-------------------------------------------------------------|----------------------|
 * | 0    | `id === boundSpoolId` — the roll already bound to this slot  | (at most one row)    |
 * | 1    | residue names THIS slot (printer + AMS unit + tray)          | `last_location_at` ↓ |
 * | 2    | residue names this printer + AMS unit, a different tray      | `last_location_at` ↓ |
 * | 3    | everything else                                             | incoming order       |
 * Tier 3 keeps the server's own `material, brand, color_name` ordering — the
 * order the operator already reads on the Inventory page. No `last_used` /
 * `loaded_at` recency chain is layered on top of it.
 *
 * A row in `blockedIds` (the modal's "bound elsewhere" knowledge: assigned to
 * another slot, or holding a stale claim on one) NEVER enters tiers 1–2. It is
 * still offered — re-binding is safe, `bind_spool_to_slot` moves rather than
 * copies — but a roll demonstrably seated somewhere else does not get to rank,
 * or be labelled, as the thing that was last in this slot.
 *
 * ## Recency labels
 * At most ONE row is labelled `'slot'` (the newest tier-1 row) and at most one
 * `'ams'` (the newest tier-2 row). A row whose `last_location_at` is null can
 * never win a label — it carries a slot breadcrumb with no time on it, so it
 * cannot be adjudicated "newest".
 *
 * This mirrors, as a DISPLAY projection, the backend's
 * `spool_binding.last_released_from_slot_stmt` contract — N rows carry the same
 * slot's residue, only the single newest is "what left this slot last", and a
 * roll bound elsewhere outranks the breadcrumb. It never decides a binding, it
 * only decides what the operator reads first.
 */
import { remainingGrams } from './spoolGrams';

/** The slot the picker was opened for. */
export interface SpoolPickerSlot {
  printerId: number;
  amsId: number;
  trayId: number;
}

/**
 * The minimal row shape the projection needs. Every field but `id` is optional
 * so Spoolman-shaped rows (which carry no residue at all) satisfy it and simply
 * fall to tier 3.
 */
export interface SpoolPickerSource {
  id: number;
  label_weight?: number | null;
  weight_used?: number | null;
  spent_at?: string | null;
  last_location_printer_id?: number | null;
  last_location_ams_id?: number | null;
  last_location_tray_id?: number | null;
  last_location_at?: string | null;
}

/** Which breadcrumb a row won, if any. */
export type SpoolRecency = 'slot' | 'ams';

export interface SpoolPickerInput<T extends SpoolPickerSource> {
  /** Already filtered by the modal's own gates (archived, tray match, search). */
  spools: readonly T[];
  slot: SpoolPickerSlot;
  /** "Show all spools" — bypasses the emptiness gate. */
  showAll: boolean;
  /** The roll bound to THIS slot, if any: it sorts first and is never blocked. */
  boundSpoolId: number | null;
  /** Rolls bound (or claiming a binding) elsewhere — barred from tiers 1–2. */
  blockedIds: ReadonlySet<number>;
}

export interface SpoolPickerResult<T> {
  /** Ordered rows to render. */
  rows: T[];
  /** How many rows the emptiness gate removed (always 0 when `showAll`). */
  hiddenEmpty: number;
  /** Spool id → its recency label. At most one entry per label value. */
  recency: ReadonlyMap<number, SpoolRecency>;
}

/**
 * Timestamp as a sortable number, or null when the row carries no usable
 * stamp. A null stamp sorts last within its tier and can never win a label.
 */
function stampOf(row: SpoolPickerSource): number | null {
  if (!row.last_location_at) return null;
  const parsed = Date.parse(row.last_location_at);
  return Number.isNaN(parsed) ? null : parsed;
}

/** True when the row's residue names this exact slot. */
function residueIsSlot(row: SpoolPickerSource, slot: SpoolPickerSlot): boolean {
  return (
    row.last_location_printer_id === slot.printerId &&
    row.last_location_ams_id === slot.amsId &&
    row.last_location_tray_id === slot.trayId
  );
}

/** True when the row's residue names this printer's AMS unit, another tray. */
function residueIsAmsUnit(row: SpoolPickerSource, slot: SpoolPickerSlot): boolean {
  return (
    row.last_location_printer_id === slot.printerId &&
    row.last_location_ams_id === slot.amsId &&
    row.last_location_tray_id !== slot.trayId &&
    row.last_location_tray_id !== null &&
    row.last_location_tray_id !== undefined
  );
}

/** The emptiness gate — see the module docstring. */
function isEmptyRoll(row: SpoolPickerSource): boolean {
  if (row.spent_at) return true;
  const label = row.label_weight ?? 0;
  if (label <= 0) return false; // unknown remaining ⇒ eligible
  return remainingGrams(row) <= 0;
}

export function pickerSpools<T extends SpoolPickerSource>(
  input: SpoolPickerInput<T>,
): SpoolPickerResult<T> {
  const { spools, slot, showAll, boundSpoolId, blockedIds } = input;

  let hiddenEmpty = 0;
  const listed: T[] = [];
  for (const row of spools) {
    if (!showAll && isEmptyRoll(row)) {
      hiddenEmpty += 1;
      continue;
    }
    listed.push(row);
  }

  const tierOf = (row: T): 0 | 1 | 2 | 3 => {
    if (boundSpoolId !== null && row.id === boundSpoolId) return 0;
    if (blockedIds.has(row.id)) return 3;
    if (residueIsSlot(row, slot)) return 1;
    if (residueIsAmsUnit(row, slot)) return 2;
    return 3;
  };

  // Decorate-sort-undecorate on a COPY: `spools` is a react-query cache array
  // and must never be reordered in place.
  const decorated = listed.map((row, index) => ({ row, index, tier: tierOf(row) }));
  decorated.sort((a, b) => {
    if (a.tier !== b.tier) return a.tier - b.tier;
    if (a.tier === 1 || a.tier === 2) {
      const left = stampOf(a.row);
      const right = stampOf(b.row);
      if (left !== right) {
        if (left === null) return 1; // stampless rows sink within the tier
        if (right === null) return -1;
        return right - left; // newest first
      }
    }
    return a.index - b.index; // stable within a tier
  });

  // Exactly one label per tier, and only for a row that carries a stamp — the
  // "single newest residue" half of the backend contract.
  const recency = new Map<number, SpoolRecency>();
  for (const label of ['slot', 'ams'] as const) {
    const tier = label === 'slot' ? 1 : 2;
    const winner = decorated.find(d => d.tier === tier && stampOf(d.row) !== null);
    if (winner) recency.set(winner.row.id, label);
  }

  return { rows: decorated.map(d => d.row), hiddenEmpty, recency };
}
