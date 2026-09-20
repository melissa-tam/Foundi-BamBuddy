/**
 * Queue display order — THE owner of how queue units are grouped and sorted.
 *
 * Every list on the queue page (the pending list, the by-target buckets, the
 * history list) reads its order from here. The page itself owns no comparator:
 * a second comparator is how the flat list, the batch children and the bucket
 * lanes drifted into three different readings of "sorted by printer".
 *
 * SCOPE — the stored order lanes on `queuePositionScopeKey`, the position
 * SCOPE, and never on target identity. A `position` is unique within its scope
 * and the backend has exactly two (`queue_builder.position_scope_of`): a
 * pinned printer, and the one shared sequence every NULL-`printer_id` row
 * lives in. Target identity cuts that shared sequence into finer lanes than
 * the backend numbers, so an appended pool run renders above older pool runs
 * that hold lower positions — and because a drag POSTS the displayed order
 * back as `ordered_ids`, which `renumber_pending` faithfully stores, a lane
 * the backend does not number by does not merely mis-display the queue, it
 * rewrites it on the first drop.
 *
 * MIRROR — the SJF comparator mirrors the scheduler's pending-item query
 * (`backend/app/services/print_scheduler.py`, the `sjf_enabled` branch of
 * `check_queue`): target lane, then `been_jumped` first, then shortest
 * `print_time_seconds` with nulls last, then `position`. Two divergences are
 * known and deliberate:
 *   1. the scheduler lanes NULL-`printer_id` rows by `target_model` alone, so
 *      two distinct printers-pool rows sharing a model share one lane there,
 *      while `queueTargetSortKey` gives every distinct pool its own lane here;
 *   2. the scheduler's NULL placement is SQL-dialect dependent, this one is not.
 * So this is the DISPLAY order, never a dispatch promise — which is why the
 * sort option stays labelled "Position" rather than naming the scheduler.
 *
 * Direction is applied ONCE, to the primary comparator only; the deterministic
 * tie-break (`queuePositionScopeKey` → `position` → id) runs AFTER it, so two
 * rows the operator cannot distinguish never swap places between renders. It
 * lanes on the scope for the same reason the stored order does: it is the
 * final say on a stored-order list, so it must not reorder across scopes by
 * anything but the number the backend assigned.
 */
import type { PrintQueueItem } from '../api/client';
import { describeQueueTarget, queuePositionScopeKey, queueTargetSortKey } from './queueTarget';
import type { PrinterNameSource, QueueTarget } from './queueTarget';
import { parseUTCDate } from './date';

/** A displayed line: one unit, or one collapsible batch of sibling units. */
export type QueueRow =
  | { kind: 'item'; item: PrintQueueItem }
  | { kind: 'batch'; batchId: number; batchName: string; items: PrintQueueItem[] };

/** The sort keys the queue and history selectors offer. */
export type QueueSortKey = 'position' | 'name' | 'printer' | 'time' | 'date';

/** A target bucket plus the rows that target it. */
export interface QueueTargetBucket extends QueueTarget {
  rows: QueueRow[];
}

export interface QueueSortContext {
  /** Translator — target labels come from `utils/queueTarget`, never from here. */
  t: (key: string) => string;
  /** Fleet names, for a printers-pool target label. */
  printerNameById: PrinterNameSource;
  /**
   * `queue_shortest_first`. It re-points the `position` key at the SJF
   * comparator and nothing else — it is a reading of "the order the scheduler
   * will use", not a hidden override of the other sort keys.
   */
  shortestFirst?: boolean;
  /**
   * History reading of the printer key: the printer the unit actually ran on
   * first, then the target label. A terminal pool unit carries `printer_name`.
   */
  history?: boolean;
  /** Name shown for a batch whose `batch_name` is empty. */
  batchFallbackName: string;
}

/** Placeholder scheduled dates (> 6 months out) mean ASAP, not "last". */
const PLACEHOLDER_HORIZON_MS = 180 * 24 * 60 * 60 * 1000;

/** Numeric collation so "Part 2" sorts before "Part 10". */
const NAME_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

function members(row: QueueRow): readonly PrintQueueItem[] {
  return row.kind === 'item' ? [row.item] : row.items;
}

function representative(row: QueueRow): PrintQueueItem | undefined {
  return row.kind === 'item' ? row.item : row.items[0];
}

function displayName(item: PrintQueueItem): string {
  return item.archive_name || item.library_file_name || '';
}

function scheduledAt(item: PrintQueueItem): number {
  if (!item.scheduled_time) return 0;
  const at = parseUTCDate(item.scheduled_time)?.getTime() ?? 0;
  return at > Date.now() + PLACEHOLDER_HORIZON_MS ? 0 : at;
}

function completedAt(item: PrintQueueItem): number {
  return parseUTCDate(item.completed_at || item.created_at)?.getTime() ?? 0;
}

function targetLabel(item: PrintQueueItem, ctx: QueueSortContext): string {
  const label = describeQueueTarget(item, ctx.t, ctx.printerNameById).label;
  return ctx.history ? item.printer_name || label : label;
}

// --- Row keys. A batch row's key is an aggregate of its members, so a group
// --- lands where its contents belong rather than where its first child does.

/** Batch: the batch name. Item: the file name. */
function rowName(row: QueueRow): string {
  return row.kind === 'item' ? displayName(row.item) : row.batchName;
}

/** Batch: its members' shared target (they are minted together). */
function rowPrinter(row: QueueRow, ctx: QueueSortContext): string {
  const rep = representative(row);
  return rep ? targetLabel(rep, ctx) : '';
}

/** Batch: the EARLIEST scheduled member — when the group first wants a printer. */
function rowTime(row: QueueRow): number {
  let earliest = Number.POSITIVE_INFINITY;
  for (const item of members(row)) earliest = Math.min(earliest, scheduledAt(item));
  return earliest;
}

/** Batch: the LATEST member terminal — when the group last did something. */
function rowDate(row: QueueRow): number {
  let latest = 0;
  for (const item of members(row)) latest = Math.max(latest, completedAt(item));
  return latest;
}

/** Batch: the LOWEST member position — where the group sits in the queue. */
function rowPosition(row: QueueRow): number {
  let lowest = Number.POSITIVE_INFINITY;
  for (const item of members(row)) lowest = Math.min(lowest, item.position);
  return lowest;
}

/** Batch: its SHORTEST member, matching what "shortest job first" promises. */
function rowPrintTime(row: QueueRow): number {
  let shortest = Number.POSITIVE_INFINITY;
  for (const item of members(row)) shortest = Math.min(shortest, item.print_time_seconds ?? Infinity);
  return shortest;
}

/** Batch: jumped if ANY member was jumped — the starvation guard is per group. */
function rowJumped(row: QueueRow): boolean {
  return members(row).some((item) => !!item.been_jumped);
}

function rowTargetKey(row: QueueRow): string {
  const rep = representative(row);
  return rep ? queueTargetSortKey(rep) : '';
}

/** Batch: its members' shared position scope — they are minted together, into
 *  one scope, so the group is numbered where its first member is. */
function rowScopeKey(row: QueueRow): string {
  const rep = representative(row);
  return rep ? queuePositionScopeKey(rep) : '';
}

function rowId(row: QueueRow): number {
  let lowest = Number.POSITIVE_INFINITY;
  for (const item of members(row)) lowest = Math.min(lowest, item.id);
  return lowest;
}

function compareTargetKey(a: QueueRow, b: QueueRow): number {
  const ak = rowTargetKey(a);
  const bk = rowTargetKey(b);
  if (ak === bk) return 0;
  return ak < bk ? -1 : 1;
}

function compareScopeKey(a: QueueRow, b: QueueRow): number {
  const ak = rowScopeKey(a);
  const bk = rowScopeKey(b);
  if (ak === bk) return 0;
  return ak < bk ? -1 : 1;
}

/** Stored order: the position SCOPE, then the position the backend assigned
 *  in it — see the SCOPE note in the module docstring for why the lane is the
 *  scope and never the target. */
function compareStoredOrder(a: QueueRow, b: QueueRow): number {
  const lane = compareScopeKey(a, b);
  if (lane !== 0) return lane;
  return rowPosition(a) - rowPosition(b);
}

/** The scheduler's own order — see the MIRROR note in the module docstring. */
function compareShortestFirst(a: QueueRow, b: QueueRow): number {
  const lane = compareTargetKey(a, b);
  if (lane !== 0) return lane;
  const jumped = Number(rowJumped(b)) - Number(rowJumped(a));
  if (jumped !== 0) return jumped;
  const at = rowPrintTime(a);
  const bt = rowPrintTime(b);
  if (at !== bt) return at - bt;
  return rowPosition(a) - rowPosition(b);
}

type RowComparator = (a: QueueRow, b: QueueRow, ctx: QueueSortContext) => number;

/** One comparator per sort key — the table IS the contract. */
const COMPARATORS: Record<QueueSortKey, RowComparator> = {
  position: (a, b, ctx) => (ctx.shortestFirst ? compareShortestFirst(a, b) : compareStoredOrder(a, b)),
  name: (a, b) => NAME_COLLATOR.compare(rowName(a), rowName(b)),
  printer: (a, b, ctx) => NAME_COLLATOR.compare(rowPrinter(a, ctx), rowPrinter(b, ctx)),
  time: (a, b) => rowTime(a) - rowTime(b),
  date: (a, b) => rowDate(a) - rowDate(b),
};

/**
 * Applied after direction, never inverted: rows the primary comparator calls
 * equal keep one stable order across renders and across both directions.
 */
function tieBreak(a: QueueRow, b: QueueRow): number {
  const lane = compareScopeKey(a, b);
  if (lane !== 0) return lane;
  const position = rowPosition(a) - rowPosition(b);
  if (position !== 0) return position;
  return rowId(a) - rowId(b);
}

function isItemRow(row: QueueRow): row is Extract<QueueRow, { kind: 'item' }> {
  return row.kind === 'item';
}

/**
 * Group units into displayed rows: siblings sharing a `batch_id` collapse into
 * one batch row, everything else stays a single-unit row. Grouping runs FIRST,
 * before any ordering or pagination, so a 120-unit batch is ONE row everywhere
 * — one line in the list, one entry on a page.
 */
export function buildRows(
  items: readonly PrintQueueItem[],
  fallbackBatchName: string,
): QueueRow[] {
  const rows: QueueRow[] = [];
  const seen = new Set<number>();
  for (const item of items) {
    const batchId = item.batch_id;
    if (batchId == null) {
      rows.push({ kind: 'item', item });
      continue;
    }
    if (seen.has(batchId)) continue;
    seen.add(batchId);
    rows.push({
      kind: 'batch',
      batchId,
      batchName: item.batch_name || fallbackBatchName,
      items: items.filter((sibling) => sibling.batch_id === batchId),
    });
  }
  return rows;
}

/**
 * Order rows by one key in one direction. Batch members are ordered by the
 * SAME comparator, so opening a group never reveals a different rule.
 */
export function sortRows(
  rows: readonly QueueRow[],
  key: QueueSortKey,
  asc: boolean,
  ctx: QueueSortContext,
): QueueRow[] {
  const compare = COMPARATORS[key];
  const ordered = rows.map((row) =>
    row.kind === 'batch' ? { ...row, items: sortItems(row.items, key, asc, ctx) } : row,
  );
  ordered.sort((a, b) => {
    const primary = compare(a, b, ctx);
    if (primary !== 0) return asc ? primary : -primary;
    return tieBreak(a, b);
  });
  return ordered;
}

/** The same order, for a flat list that carries no grouping (active prints). */
export function sortItems(
  items: readonly PrintQueueItem[],
  key: QueueSortKey,
  asc: boolean,
  ctx: QueueSortContext,
): PrintQueueItem[] {
  const rows: QueueRow[] = items.map((item) => ({ kind: 'item', item }));
  return sortRows(rows, key, asc, ctx).filter(isItemRow).map((row) => row.item);
}

/**
 * Bucket units by target, then group and order each bucket's rows. Buckets
 * themselves ride `queueTargetSortKey`, with unassigned forced last — it is
 * the lane an operator must act on, not one the scheduler will drain.
 */
export function bucketRowsByTarget(
  items: readonly PrintQueueItem[],
  key: QueueSortKey,
  asc: boolean,
  ctx: QueueSortContext,
): QueueTargetBucket[] {
  const buckets = new Map<string, { target: QueueTarget; sortKey: string; items: PrintQueueItem[] }>();
  for (const item of items) {
    const target = describeQueueTarget(item, ctx.t, ctx.printerNameById);
    const existing = buckets.get(target.key);
    if (existing) {
      existing.items.push(item);
    } else {
      buckets.set(target.key, { target, sortKey: queueTargetSortKey(item), items: [item] });
    }
  }
  const out = Array.from(buckets.values()).map((bucket) => ({
    sortKey: bucket.sortKey,
    bucket: {
      ...bucket.target,
      rows: sortRows(buildRows(bucket.items, ctx.batchFallbackName), key, asc, ctx),
    },
  }));
  out.sort((a, b) => {
    const aUnassigned = a.bucket.kind === 'unassigned';
    const bUnassigned = b.bucket.kind === 'unassigned';
    if (aUnassigned !== bUnassigned) return aUnassigned ? 1 : -1;
    if (a.sortKey === b.sortKey) return 0;
    return a.sortKey < b.sortKey ? -1 : 1;
  });
  return out.map((entry) => entry.bucket);
}
