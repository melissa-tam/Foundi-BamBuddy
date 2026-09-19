/**
 * queueSort: THE owner of queue display order.
 *
 * The defects these pin, all shipped at once on the old page-local sorts:
 * "by printer" read `printer_name`, which a pool unit does not have, so every
 * unassigned/pool row collapsed into one nameless clump; SJF silently overrode
 * the operator's chosen key instead of only re-reading Position; batches were
 * grouped AFTER sorting, so a 120-unit run was 120 rows; and equal rows
 * reshuffled between renders because nothing broke the tie.
 */
import { describe, it, expect } from 'vitest';
import type { PrintQueueItem } from '../../api/client';
import {
  buildRows,
  bucketRowsByTarget,
  sortItems,
  sortRows,
  type QueueRow,
  type QueueSortContext,
} from '../../utils/queueSort';

// Echo the key so labels read as `<key> <names>` without the i18n runtime.
const t = (k: string) => k;

const printerNameById = new Map<number, string>([
  [1, 'Alpha'],
  [2, 'Bravo'],
  [3, 'Charlie'],
]);

function ctx(overrides: Partial<QueueSortContext> = {}): QueueSortContext {
  return {
    t,
    printerNameById,
    batchFallbackName: 'Batch',
    ...overrides,
  };
}

function item(overrides: Partial<PrintQueueItem> & { id: number }): PrintQueueItem {
  return {
    printer_id: null,
    target_model: null,
    target_printer_ids: null,
    target_location: null,
    required_filament_types: null,
    waiting_reason: null,
    archive_id: null,
    library_file_id: null,
    position: 1,
    scheduled_time: null,
    require_previous_success: false,
    auto_off_after: false,
    manual_start: false,
    filament_short: false,
    skip_filament_check: false,
    ams_mapping: null,
    filament_overrides: null,
    plate_id: null,
    bed_levelling: true,
    flow_cali: false,
    vibration_cali: true,
    layer_inspect: false,
    timelapse: false,
    use_ams: true,
    nozzle_offset_cali: false,
    status: 'pending',
    started_at: null,
    completed_at: null,
    error_message: null,
    created_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

/** Ids in row order; a batch row contributes `batch:<id>`. */
function order(rows: QueueRow[]): string[] {
  return rows.map((row) => (row.kind === 'item' ? String(row.item.id) : `batch:${row.batchId}`));
}

describe('buildRows', () => {
  it('collapses a batch into ONE row regardless of its size', () => {
    const units = Array.from({ length: 120 }, (_, i) =>
      item({ id: i + 1, position: i + 1, batch_id: 9, batch_name: 'SKU007 run' }),
    );
    const rows = buildRows(units, 'Batch');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ kind: 'batch', batchId: 9, batchName: 'SKU007 run' });
    expect(rows[0].kind === 'batch' && rows[0].items).toHaveLength(120);
  });

  it('keeps 60 batches as 60 rows — the unit count is not the row count', () => {
    const units = Array.from({ length: 60 }, (_, b) =>
      [1, 2].map((n) => item({ id: b * 10 + n, position: b * 10 + n, batch_id: b + 1 })),
    ).flat();
    const rows = buildRows(units, 'Batch');
    expect(rows).toHaveLength(60);
    // Paging rows (not units) is what a page of history shows.
    expect(rows.slice(0, 50)).toHaveLength(50);
  });

  it('names an unnamed batch with the caller-supplied fallback', () => {
    const rows = buildRows([item({ id: 1, batch_id: 4, batch_name: null })], 'Batch');
    expect(rows[0]).toMatchObject({ kind: 'batch', batchName: 'Batch' });
  });

  it('leaves un-batched units as their own rows, in input order', () => {
    const rows = buildRows([item({ id: 2 }), item({ id: 1 })], 'Batch');
    expect(order(rows)).toEqual(['2', '1']);
  });
});

describe('sortRows — position', () => {
  const units = [
    item({ id: 1, printer_id: 1, printer_name: 'Alpha', position: 3 }),
    item({ id: 2, printer_id: 1, printer_name: 'Alpha', position: 1 }),
    item({ id: 3, printer_id: 1, printer_name: 'Alpha', position: 2 }),
  ];

  it('orders by stored position ascending', () => {
    expect(order(sortRows(buildRows(units, 'Batch'), 'position', true, ctx()))).toEqual(['2', '3', '1']);
  });

  it('reverses on descending', () => {
    expect(order(sortRows(buildRows(units, 'Batch'), 'position', false, ctx()))).toEqual(['1', '3', '2']);
  });

  it('lanes pool units ahead of pinned ones, unassigned between, per queueTargetSortKey', () => {
    const mixed = [
      item({ id: 1, printer_id: 2, printer_name: 'Bravo', position: 1 }),
      item({ id: 2, position: 1 }),
      item({ id: 3, target_model: 'H2S', position: 1 }),
    ];
    expect(order(sortRows(buildRows(mixed, 'Batch'), 'position', true, ctx()))).toEqual(['3', '2', '1']);
  });

  it('gives a batch its LOWEST member position', () => {
    const rows = buildRows(
      [
        item({ id: 1, position: 5 }),
        item({ id: 2, position: 2, batch_id: 7 }),
        item({ id: 3, position: 9, batch_id: 7 }),
      ],
      'Batch',
    );
    expect(order(sortRows(rows, 'position', true, ctx()))).toEqual(['batch:7', '1']);
  });
});

describe('sortRows — SJF reads Position, and only Position', () => {
  const units = [
    item({ id: 1, printer_id: 1, printer_name: 'Alpha', position: 1, print_time_seconds: 7200 }),
    item({ id: 2, printer_id: 1, printer_name: 'Alpha', position: 2, print_time_seconds: 600 }),
    item({ id: 3, printer_id: 1, printer_name: 'Alpha', position: 3, print_time_seconds: null }),
  ];

  it('orders shortest first, nulls last', () => {
    const rows = sortRows(buildRows(units, 'Batch'), 'position', true, ctx({ shortestFirst: true }));
    expect(order(rows)).toEqual(['2', '1', '3']);
  });

  it('floats a jumped unit above a shorter one (the starvation guard)', () => {
    const jumped = [...units, item({ id: 4, printer_id: 1, printer_name: 'Alpha', position: 9, print_time_seconds: 99999, been_jumped: true })];
    const rows = sortRows(buildRows(jumped, 'Batch'), 'position', true, ctx({ shortestFirst: true }));
    expect(order(rows)[0]).toBe('4');
  });

  it('does NOT hijack the other sort keys', () => {
    const named = [
      item({ id: 1, archive_name: 'Zebra', print_time_seconds: 10 }),
      item({ id: 2, archive_name: 'Apple', print_time_seconds: 9999 }),
    ];
    const rows = sortRows(buildRows(named, 'Batch'), 'name', true, ctx({ shortestFirst: true }));
    expect(order(rows)).toEqual(['2', '1']);
  });
});

describe('sortRows — name', () => {
  it('sorts A→Z ascending and Z→A descending', () => {
    const units = [
      item({ id: 1, archive_name: 'Cog' }),
      item({ id: 2, archive_name: 'Axle' }),
      item({ id: 3, library_file_name: 'Bracket' }),
    ];
    expect(order(sortRows(buildRows(units, 'Batch'), 'name', true, ctx()))).toEqual(['2', '3', '1']);
    expect(order(sortRows(buildRows(units, 'Batch'), 'name', false, ctx()))).toEqual(['1', '3', '2']);
  });

  it('collates numerically — Part 2 before Part 10', () => {
    const units = [item({ id: 1, archive_name: 'Part 10' }), item({ id: 2, archive_name: 'Part 2' })];
    expect(order(sortRows(buildRows(units, 'Batch'), 'name', true, ctx()))).toEqual(['2', '1']);
  });

  it('sorts a batch by its BATCH name, not its first child', () => {
    const units = [
      item({ id: 1, archive_name: 'Aardvark' }),
      item({ id: 2, archive_name: 'Aardvark', batch_id: 5, batch_name: 'Zulu run' }),
    ];
    expect(order(sortRows(buildRows(units, 'Batch'), 'name', true, ctx()))).toEqual(['1', 'batch:5']);
  });

  it('orders batch members by the same comparator', () => {
    const units = [
      item({ id: 1, archive_name: 'Delta', batch_id: 5 }),
      item({ id: 2, archive_name: 'Bravo', batch_id: 5 }),
    ];
    const rows = sortRows(buildRows(units, 'Batch'), 'name', true, ctx());
    expect(rows[0].kind === 'batch' && rows[0].items.map((i) => i.id)).toEqual([2, 1]);
  });
});

describe('sortRows — printer', () => {
  it('names a POOL unit by its target label, not by a printer_name it lacks', () => {
    const units = [
      item({ id: 1, printer_id: 2, printer_name: 'Bravo' }),
      item({ id: 2, target_printer_ids: [1, 3] }),
      item({ id: 3 }),
    ];
    const rows = sortRows(buildRows(units, 'Batch'), 'printer', true, ctx());
    // 'Bravo' < 'queue.filter.anyOf Alpha, Charlie' < 'queue.filter.unassigned'
    expect(order(rows)).toEqual(['1', '2', '3']);
  });

  it('reverses cleanly — direction is applied once', () => {
    const units = [
      item({ id: 1, printer_id: 1, printer_name: 'Alpha' }),
      item({ id: 2, printer_id: 2, printer_name: 'Bravo' }),
    ];
    expect(order(sortRows(buildRows(units, 'Batch'), 'printer', false, ctx()))).toEqual(['2', '1']);
  });

  it('history reads the printer the unit actually ran on', () => {
    const units = [
      item({ id: 1, status: 'completed', printer_id: 3, printer_name: 'Charlie' }),
      item({ id: 2, status: 'completed', printer_id: 1, printer_name: 'Alpha' }),
    ];
    const rows = sortRows(buildRows(units, 'Batch'), 'printer', true, ctx({ history: true }));
    expect(order(rows)).toEqual(['2', '1']);
  });
});

describe('sortRows — time', () => {
  it('orders by scheduled start, with ASAP first', () => {
    const units = [
      item({ id: 1, scheduled_time: '2026-02-01T10:00:00Z' }),
      item({ id: 2, scheduled_time: null }),
      item({ id: 3, scheduled_time: '2026-01-05T10:00:00Z' }),
    ];
    expect(order(sortRows(buildRows(units, 'Batch'), 'time', true, ctx()))).toEqual(['2', '3', '1']);
  });

  it('treats a placeholder date far in the future as ASAP', () => {
    const faraway = new Date(Date.now() + 400 * 24 * 3600 * 1000).toISOString();
    const units = [
      item({ id: 1, scheduled_time: '2026-01-05T10:00:00Z' }),
      item({ id: 2, scheduled_time: faraway }),
    ];
    expect(order(sortRows(buildRows(units, 'Batch'), 'time', true, ctx()))).toEqual(['2', '1']);
  });

  it('gives a batch its EARLIEST member', () => {
    const units = [
      item({ id: 1, scheduled_time: '2026-01-02T00:00:00Z' }),
      item({ id: 2, scheduled_time: '2026-03-01T00:00:00Z', batch_id: 3 }),
      item({ id: 3, scheduled_time: '2026-01-01T00:00:00Z', batch_id: 3 }),
    ];
    expect(order(sortRows(buildRows(units, 'Batch'), 'time', true, ctx()))).toEqual(['batch:3', '1']);
  });
});

describe('sortRows — date', () => {
  const units = [
    item({ id: 1, status: 'completed', completed_at: '2026-01-02T00:00:00Z' }),
    item({ id: 2, status: 'failed', completed_at: '2026-01-03T00:00:00Z' }),
    item({ id: 3, status: 'cancelled', completed_at: null, created_at: '2026-01-01T00:00:00Z' }),
  ];

  it('ascending is oldest first', () => {
    expect(order(sortRows(buildRows(units, 'Batch'), 'date', true, ctx({ history: true })))).toEqual(['3', '1', '2']);
  });

  it('descending is newest first — the history default', () => {
    expect(order(sortRows(buildRows(units, 'Batch'), 'date', false, ctx({ history: true })))).toEqual(['2', '1', '3']);
  });

  it('gives a batch its LATEST member', () => {
    const batched = [
      item({ id: 1, status: 'completed', completed_at: '2026-01-05T00:00:00Z' }),
      item({ id: 2, status: 'completed', completed_at: '2026-01-01T00:00:00Z', batch_id: 8 }),
      item({ id: 3, status: 'completed', completed_at: '2026-01-09T00:00:00Z', batch_id: 8 }),
    ];
    expect(order(sortRows(buildRows(batched, 'Batch'), 'date', false, ctx({ history: true })))).toEqual(['batch:8', '1']);
  });
});

describe('sortRows — ties', () => {
  it('breaks a tie by target, then position, then id — in BOTH directions', () => {
    const units = [
      item({ id: 9, archive_name: 'Same', printer_id: 1, printer_name: 'Alpha', position: 2 }),
      item({ id: 4, archive_name: 'Same', printer_id: 1, printer_name: 'Alpha', position: 1 }),
      item({ id: 7, archive_name: 'Same', printer_id: 1, printer_name: 'Alpha', position: 1 }),
    ];
    const asc = order(sortRows(buildRows(units, 'Batch'), 'name', true, ctx()));
    const desc = order(sortRows(buildRows(units, 'Batch'), 'name', false, ctx()));
    expect(asc).toEqual(['4', '7', '9']);
    // The tie-break is NOT inverted with the direction: equal rows keep one order.
    expect(desc).toEqual(asc);
  });

  it('does not mutate its input', () => {
    const units = [item({ id: 2, position: 2 }), item({ id: 1, position: 1 })];
    const rows = buildRows(units, 'Batch');
    const before = order(rows);
    sortRows(rows, 'position', true, ctx());
    expect(order(rows)).toEqual(before);
  });
});

describe('sortItems', () => {
  it('applies the same order to a flat list (the active-prints section)', () => {
    const units = [
      item({ id: 1, status: 'printing', archive_name: 'Yak' }),
      item({ id: 2, status: 'printing', archive_name: 'Ant' }),
    ];
    expect(sortItems(units, 'name', true, ctx()).map((i) => i.id)).toEqual([2, 1]);
  });
});

describe('bucketRowsByTarget', () => {
  it('buckets by target key, orders lanes by queueTargetSortKey, unassigned LAST', () => {
    const units = [
      item({ id: 1 }),
      item({ id: 2, printer_id: 2, printer_name: 'Bravo' }),
      item({ id: 3, target_model: 'H2S' }),
      item({ id: 4, printer_id: 1, printer_name: 'Alpha' }),
    ];
    const buckets = bucketRowsByTarget(units, 'position', true, ctx());
    expect(buckets.map((b) => b.key)).toEqual([
      'model:H2S',
      'printer:1',
      'printer:2',
      'unassigned',
    ]);
  });

  it('gives each distinct printers-pool its own lane', () => {
    const units = [
      item({ id: 1, target_printer_ids: [1, 2] }),
      item({ id: 2, target_printer_ids: [2, 3] }),
    ];
    const buckets = bucketRowsByTarget(units, 'position', true, ctx());
    expect(buckets).toHaveLength(2);
    expect(buckets.map((b) => b.key)).toEqual(['printers:1,2', 'printers:2,3']);
  });

  it('groups batches INSIDE each bucket and sorts the bucket rows', () => {
    const units = [
      item({ id: 1, printer_id: 1, printer_name: 'Alpha', position: 5 }),
      item({ id: 2, printer_id: 1, printer_name: 'Alpha', position: 1, batch_id: 6 }),
      item({ id: 3, printer_id: 1, printer_name: 'Alpha', position: 2, batch_id: 6 }),
    ];
    const buckets = bucketRowsByTarget(units, 'position', true, ctx());
    expect(buckets).toHaveLength(1);
    expect(order(buckets[0].rows)).toEqual(['batch:6', '1']);
  });
});
