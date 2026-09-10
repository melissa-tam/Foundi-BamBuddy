import { describe, it, expect } from 'vitest';
import { pickerSpools, type SpoolPickerSource } from '../../utils/spoolPicker';

const SLOT = { printerId: 1, amsId: 0, trayId: 2 };

function row(id: number, extra: Partial<SpoolPickerSource> = {}): SpoolPickerSource {
  return { id, label_weight: 1000, weight_used: 0, ...extra };
}

/** Residue naming a given slot. */
function residue(printerId: number, amsId: number, trayId: number, at: string | null) {
  return {
    last_location_printer_id: printerId,
    last_location_ams_id: amsId,
    last_location_tray_id: trayId,
    last_location_at: at,
  };
}

function pick(spools: readonly SpoolPickerSource[], overrides: Partial<Parameters<typeof pickerSpools>[0]> = {}) {
  return pickerSpools({
    spools,
    slot: SLOT,
    showAll: false,
    boundSpoolId: null,
    blockedIds: new Set<number>(),
    ...overrides,
  });
}

describe('pickerSpools — emptiness gate', () => {
  it('hides a spent roll and counts it', () => {
    const result = pick([row(1), row(2, { spent_at: '2026-09-01T00:00:00Z' })]);
    expect(result.rows.map(r => r.id)).toEqual([1]);
    expect(result.hiddenEmpty).toBe(1);
  });

  it('hides a roll whose label is known and remaining is 0', () => {
    const result = pick([row(1), row(2, { label_weight: 1000, weight_used: 1000 })]);
    expect(result.rows.map(r => r.id)).toEqual([1]);
    expect(result.hiddenEmpty).toBe(1);
  });

  it('hides a roll charged past its label', () => {
    const result = pick([row(2, { label_weight: 1000, weight_used: 1200 })]);
    expect(result.rows).toHaveLength(0);
    expect(result.hiddenEmpty).toBe(1);
  });

  it('lists a roll with no label weight — unknown remaining is eligible', () => {
    const result = pick([
      row(1, { label_weight: null, weight_used: 5000 }),
      row(2, { label_weight: 0, weight_used: 5000 }),
      { id: 3 },
    ]);
    expect(result.rows.map(r => r.id)).toEqual([1, 2, 3]);
    expect(result.hiddenEmpty).toBe(0);
  });

  it('lists everything and hides nothing when showAll is set', () => {
    const result = pick(
      [row(1), row(2, { spent_at: '2026-09-01T00:00:00Z' }), row(3, { weight_used: 1000 })],
      { showAll: true },
    );
    expect(result.rows.map(r => r.id)).toEqual([1, 2, 3]);
    expect(result.hiddenEmpty).toBe(0);
  });
});

describe('pickerSpools — order', () => {
  it('floats the roll bound to this slot to tier 0, ahead of a slot residue', () => {
    const result = pick(
      [
        row(1),
        row(2, residue(1, 0, 2, '2026-09-09T10:00:00Z')),
        row(3),
      ],
      { boundSpoolId: 3 },
    );
    expect(result.rows.map(r => r.id)).toEqual([3, 2, 1]);
  });

  it('ranks this slot’s residue (tier 1) above the AMS unit’s (tier 2) above the rest', () => {
    const result = pick([
      row(1),
      row(2, residue(1, 0, 3, '2026-09-09T10:00:00Z')), // same AMS unit, other tray
      row(3, residue(1, 0, 2, '2026-09-09T09:00:00Z')), // this slot
    ]);
    expect(result.rows.map(r => r.id)).toEqual([3, 2, 1]);
  });

  it('orders within a residue tier by last_location_at, newest first', () => {
    const result = pick([
      row(1, residue(1, 0, 2, '2026-09-01T00:00:00Z')),
      row(2, residue(1, 0, 2, '2026-09-09T00:00:00Z')),
      row(3, residue(1, 0, 2, '2026-09-05T00:00:00Z')),
    ]);
    expect(result.rows.map(r => r.id)).toEqual([2, 3, 1]);
  });

  it('sinks a stampless residue row to the bottom of its tier', () => {
    const result = pick([
      row(1, residue(1, 0, 2, null)),
      row(2, residue(1, 0, 2, '2026-09-01T00:00:00Z')),
    ]);
    expect(result.rows.map(r => r.id)).toEqual([2, 1]);
  });

  it('ignores residue naming a different printer or AMS unit', () => {
    const result = pick([
      row(1, residue(2, 0, 2, '2026-09-09T00:00:00Z')), // other printer
      row(2, residue(1, 1, 2, '2026-09-09T00:00:00Z')), // other AMS unit
      row(3, residue(1, 0, 2, '2026-08-01T00:00:00Z')), // this slot, older
    ]);
    expect(result.rows.map(r => r.id)).toEqual([3, 1, 2]);
    expect(result.recency.get(1)).toBeUndefined();
    expect(result.recency.get(2)).toBeUndefined();
  });

  it('keeps tier-3 rows in the incoming (server) order', () => {
    const result = pick([row(7), row(3), row(5), row(1)]);
    expect(result.rows.map(r => r.id)).toEqual([7, 3, 5, 1]);
  });

  it('treats Spoolman-shaped rows (no residue fields) as tier 3', () => {
    const result = pick([
      { id: 10, label_weight: 1000, weight_used: 0 },
      row(11, residue(1, 0, 2, '2026-09-09T00:00:00Z')),
    ]);
    expect(result.rows.map(r => r.id)).toEqual([11, 10]);
    expect(result.recency.get(10)).toBeUndefined();
  });
});

describe('pickerSpools — blockedIds', () => {
  it('keeps a blocked slot-residue row at tier 3 and unlabelled', () => {
    const result = pick(
      [
        row(1, residue(1, 0, 2, '2026-09-09T00:00:00Z')), // blocked: bound elsewhere
        row(2, residue(1, 0, 2, '2026-09-01T00:00:00Z')),
      ],
      { blockedIds: new Set([1]) },
    );
    expect(result.rows.map(r => r.id)).toEqual([2, 1]);
    expect(result.recency.get(2)).toBe('slot');
    expect(result.recency.get(1)).toBeUndefined();
  });

  it('never blocks the roll bound to this slot', () => {
    const result = pick([row(1), row(9)], { boundSpoolId: 9, blockedIds: new Set([9]) });
    expect(result.rows.map(r => r.id)).toEqual([9, 1]);
  });
});

describe('pickerSpools — recency labels', () => {
  it('labels exactly one slot row and one AMS row, the newest of each', () => {
    const result = pick([
      row(1, residue(1, 0, 2, '2026-09-01T00:00:00Z')),
      row(2, residue(1, 0, 2, '2026-09-09T00:00:00Z')),
      row(3, residue(1, 0, 3, '2026-09-02T00:00:00Z')),
      row(4, residue(1, 0, 1, '2026-09-08T00:00:00Z')),
      row(5),
    ]);
    expect([...result.recency.entries()].sort()).toEqual([[2, 'slot'], [4, 'ams']].sort());
  });

  it('never labels a row whose last_location_at is null', () => {
    const result = pick([
      row(1, residue(1, 0, 2, null)),
      row(2, residue(1, 0, 3, null)),
    ]);
    expect(result.recency.size).toBe(0);
  });

  it('falls through to a stamped row when the first residue row has no stamp', () => {
    const result = pick([
      row(1, residue(1, 0, 2, null)),
      row(2, residue(1, 0, 2, '2026-09-01T00:00:00Z')),
    ]);
    expect(result.recency.get(2)).toBe('slot');
    expect(result.recency.get(1)).toBeUndefined();
  });

  it('does not label a row hidden by the emptiness gate', () => {
    const result = pick([
      row(1, { ...residue(1, 0, 2, '2026-09-09T00:00:00Z'), spent_at: '2026-09-09T01:00:00Z' }),
      row(2, residue(1, 0, 2, '2026-09-01T00:00:00Z')),
    ]);
    expect(result.rows.map(r => r.id)).toEqual([2]);
    expect(result.recency.get(2)).toBe('slot');
  });
});

describe('pickerSpools — purity', () => {
  it('never mutates or reorders the input array', () => {
    const input = Object.freeze([
      row(1),
      row(2, residue(1, 0, 2, '2026-09-09T00:00:00Z')),
    ]) as readonly SpoolPickerSource[];

    // A frozen array throws on any in-place sort/splice, so this both asserts
    // the copy AND proves the react-query cache array is safe to pass.
    const result = pick(input);

    expect(result.rows.map(r => r.id)).toEqual([2, 1]);
    expect(input.map(r => r.id)).toEqual([1, 2]);
  });
});
