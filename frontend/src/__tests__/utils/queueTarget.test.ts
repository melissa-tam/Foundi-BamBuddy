/**
 * describeQueueTarget / queueTargetSortKey: the ONE origin for "what does this
 * queue unit target". The queue rows, the printer buckets and the timeline
 * lanes all read it, so its precedence must mirror the backend `target_of`
 * exactly — a pinned printer, else a printers pool, else a model pool, else
 * unassigned — and its key must stay stable enough to group by.
 */
import { describe, it, expect } from 'vitest';
import {
  describeQueueTarget,
  queuePositionScopeKey,
  queueTargetKindClasses,
  queueTargetSortKey,
  type QueueTargetItem,
} from '../../utils/queueTarget';

// Echo the key so the mapping is asserted without the i18n runtime; the label
// assertions then read as `<key> <names>`.
const t = (k: string) => k;

const names = new Map<number, string>([
  [1, 'H2S-Alpha'],
  [2, 'H2S-Beta'],
  [7, 'H2C-Gamma'],
]);

function item(overrides: Partial<QueueTargetItem> = {}): QueueTargetItem {
  return {
    printer_id: null,
    printer_name: null,
    target_model: null,
    target_printer_ids: null,
    ...overrides,
  };
}

describe('describeQueueTarget', () => {
  it('reads a pinned printer from the item, by name', () => {
    const target = describeQueueTarget(
      item({ printer_id: 2, printer_name: 'H2S-Beta' }),
      t,
      names,
    );
    expect(target).toEqual({ kind: 'printer', key: 'printer:2', label: 'H2S-Beta' });
  });

  it('falls back to "Printer #id" when the item carries no name', () => {
    const target = describeQueueTarget(item({ printer_id: 4 }), t, names);
    expect(target.kind).toBe('printer');
    expect(target.label).toBe('common.printer #4');
  });

  it('names every member of a printers pool, in the order received', () => {
    const target = describeQueueTarget(item({ target_printer_ids: [7, 1] }), t, names);
    // Ids are used AS RECEIVED — the backend sends them canonical, so the
    // label must not re-sort them into 1, 7.
    expect(target).toEqual({
      kind: 'printers',
      key: 'printers:7,1',
      label: 'queue.filter.anyOf H2C-Gamma, H2S-Alpha',
    });
  });

  it('falls back to #id for a pool member missing from the fleet map', () => {
    const target = describeQueueTarget(item({ target_printer_ids: [1, 99] }), t, names);
    expect(target.label).toBe('queue.filter.anyOf H2S-Alpha, #99');
  });

  it('accepts a plain record as the name source', () => {
    const target = describeQueueTarget(
      item({ target_printer_ids: [1, 2] }),
      t,
      { 1: 'H2S-Alpha', 2: 'H2S-Beta' },
    );
    expect(target.label).toBe('queue.filter.anyOf H2S-Alpha, H2S-Beta');
  });

  it('reads a model pool', () => {
    const target = describeQueueTarget(item({ target_model: 'H2S' }), t, names);
    expect(target).toEqual({
      kind: 'model',
      key: 'model:H2S',
      label: 'queue.filter.any H2S',
    });
  });

  it('reads an unassigned unit', () => {
    const target = describeQueueTarget(item(), t, names);
    expect(target).toEqual({
      kind: 'unassigned',
      key: 'unassigned',
      label: 'queue.filter.unassigned',
    });
  });

  it('treats an empty printers pool as no pool at all', () => {
    expect(describeQueueTarget(item({ target_printer_ids: [] }), t, names).kind).toBe(
      'unassigned',
    );
    expect(
      describeQueueTarget(item({ target_printer_ids: [], target_model: 'H2S' }), t, names).kind,
    ).toBe('model');
  });

  describe('precedence (mirrors the backend target_of)', () => {
    it('prefers a pinned printer over both pools', () => {
      const target = describeQueueTarget(
        item({
          printer_id: 1,
          printer_name: 'H2S-Alpha',
          target_printer_ids: [1, 2],
          target_model: 'H2S',
        }),
        t,
        names,
      );
      expect(target.kind).toBe('printer');
      expect(target.key).toBe('printer:1');
    });

    it('prefers a printers pool over a model pool', () => {
      const target = describeQueueTarget(
        item({ target_printer_ids: [1, 2], target_model: 'H2S' }),
        t,
        names,
      );
      expect(target.kind).toBe('printers');
      expect(target.key).toBe('printers:1,2');
    });
  });

  it('keys the same pool identically regardless of the fleet names known', () => {
    const a = describeQueueTarget(item({ target_printer_ids: [1, 2] }), t, names);
    const b = describeQueueTarget(item({ target_printer_ids: [1, 2] }), t, new Map());
    expect(b.key).toBe(a.key);
    // Only the label degrades when a name is unknown.
    expect(b.label).toBe('queue.filter.anyOf #1, #2');
  });

  it('gives two different pools two different keys', () => {
    const a = describeQueueTarget(item({ target_printer_ids: [1, 2] }), t, names);
    const b = describeQueueTarget(item({ target_printer_ids: [1, 7] }), t, names);
    expect(a.key).not.toBe(b.key);
  });
});

describe('queueTargetSortKey', () => {
  const sorted = (items: QueueTargetItem[]) =>
    [...items].sort((a, b) => {
      const ka = queueTargetSortKey(a);
      const kb = queueTargetSortKey(b);
      return ka === kb ? 0 : ka < kb ? -1 : 1;
    });

  it('orders the pool lanes first (the scheduler order), then unassigned, then pinned printers', () => {
    const unassigned = item();
    const model = item({ target_model: 'H2S' });
    const pool = item({ target_printer_ids: [1, 2] });
    const pinned = item({ printer_id: 3 });

    // Pool lanes group by key: `model:H2S` sorts before `printers:1,2`.
    expect(sorted([pinned, unassigned, pool, model]).map((i) => queueTargetSortKey(i))).toEqual([
      queueTargetSortKey(model),
      queueTargetSortKey(pool),
      queueTargetSortKey(unassigned),
      queueTargetSortKey(pinned),
    ]);
  });

  it('orders pinned printers numerically, not lexically', () => {
    const p2 = item({ printer_id: 2 });
    const p10 = item({ printer_id: 10 });
    expect(sorted([p10, p2])).toEqual([p2, p10]);
  });

  it('groups units of one pool together and separates distinct pools', () => {
    const poolA1 = item({ target_printer_ids: [1, 2] });
    const poolA2 = item({ target_printer_ids: [1, 2] });
    const poolB = item({ target_printer_ids: [1, 7] });
    expect(queueTargetSortKey(poolA1)).toBe(queueTargetSortKey(poolA2));
    expect(queueTargetSortKey(poolA1)).not.toBe(queueTargetSortKey(poolB));
  });

  it('separates two models sharing a first letter (the charCodeAt collision)', () => {
    expect(queueTargetSortKey(item({ target_model: 'H2S' }))).not.toBe(
      queueTargetSortKey(item({ target_model: 'H2C' })),
    );
  });

  it('separates a printers pool from unassigned (they used to collide)', () => {
    expect(queueTargetSortKey(item({ target_printer_ids: [1] }))).not.toBe(
      queueTargetSortKey(item()),
    );
  });
});

/**
 * The scope a `position` is numbered within — the mirror of backend
 * `queue_builder.position_scope_of`. Exactly TWO shapes, and the difference
 * from `queueTargetSortKey` is the whole point: a target is not a scope.
 */
describe('queuePositionScopeKey', () => {
  it('puts all four target shapes into the two scopes the backend numbers', () => {
    // Every NULL-printer shape shares ONE sequence...
    expect(queuePositionScopeKey(item({ target_model: 'H2S' }))).toBe('0:shared');
    expect(queuePositionScopeKey(item({ target_printer_ids: [1, 2] }))).toBe('0:shared');
    expect(queuePositionScopeKey(item())).toBe('0:shared');
    // ...and a pin is numbered against its own printer.
    expect(queuePositionScopeKey(item({ printer_id: 7 }))).toBe('1:0000000007');
  });

  it('gives the three NULL-printer shapes ONE key, unlike the target key', () => {
    const model = item({ target_model: 'H2S' });
    const pool = item({ target_printer_ids: [1, 2] });
    const unassigned = item();
    expect(new Set([model, pool, unassigned].map(queuePositionScopeKey)).size).toBe(1);
    // The defect this closes: the target key gives them three lanes, so a
    // stored-order sort laned by it reorders rows the backend numbered as one.
    expect(new Set([model, pool, unassigned].map(queueTargetSortKey)).size).toBe(3);
  });

  it('sorts the shared sequence ahead of every pinned scope', () => {
    // The scheduler's `ORDER BY printer_id, position` yields NULL rows first
    // on SQLite, so the display order follows.
    expect(queuePositionScopeKey(item()) < queuePositionScopeKey(item({ printer_id: 1 }))).toBe(true);
  });

  it('orders pinned scopes numerically, not lexically', () => {
    expect(
      queuePositionScopeKey(item({ printer_id: 2 })) < queuePositionScopeKey(item({ printer_id: 10 })),
    ).toBe(true);
  });

  it('reads the pin alone — a pinned row keeps its scope whatever pools it names', () => {
    expect(
      queuePositionScopeKey(item({ printer_id: 3, target_model: 'H2S', target_printer_ids: [1, 2] })),
    ).toBe('1:0000000003');
  });
});

describe('queueTargetKindClasses', () => {
  it('is ONE table — the pools share a colour, the other kinds each own one', () => {
    expect(queueTargetKindClasses('model')).toEqual(queueTargetKindClasses('printers'));
    const rails = (['printer', 'model', 'unassigned'] as const).map(
      (kind) => queueTargetKindClasses(kind).rail,
    );
    expect(new Set(rails).size).toBe(3);
  });

  it('pairs a light and a dark token for every kind the theme tints', () => {
    for (const kind of ['model', 'printers', 'unassigned'] as const) {
      const classes = queueTargetKindClasses(kind);
      expect(classes.chip).toMatch(/dark:/);
      expect(classes.rail).toMatch(/dark:/);
    }
  });
});
