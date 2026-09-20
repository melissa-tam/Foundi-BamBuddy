/**
 * What a queue unit targets — the ONE frontend origin for that question.
 *
 * A unit is dispatched against exactly one target — a pinned `printer_id`, a
 * `target_printer_ids` pool, a `target_model` pool, or nothing. This is the
 * DISPLAY reading of those columns and its precedence is deliberately not the
 * backend discriminator's: the backend (`services/dispatch_target.target_of`)
 * classifies a row by its pool columns first, because a printing pool unit is
 * still a pool unit for dispatch semantics; a view instead shows WHERE the unit
 * is, so a non-null `printer_id` wins here. A pending pool unit never carries a
 * `printer_id` (it is written only at `pending -> printing`), so the two
 * readings agree on every row that is still waiting. Every view that groups,
 * sorts or labels queue units calls in here rather than re-deriving it, so a
 * row and its lane can never disagree.
 *
 * `printer_id` becomes non-null only once a pool unit is printing/terminal, so
 * a unit MOVES from a pool target to a printer target over its lifetime; the
 * key changes with it, which is the intended regrouping.
 *
 * The returned `label` is the target alone. A caller that shows more about the
 * unit (`@ location`, a filament list) composes those suffixes itself.
 */
import type { PrintQueueItem } from '../api/client';

/** Which of the four target shapes a unit carries. */
export type QueueTargetKind = 'printer' | 'model' | 'printers' | 'unassigned';

export interface QueueTarget {
  kind: QueueTargetKind;
  /** Stable grouping identity: `printer:<id>`, `printers:<ids>`,
   *  `model:<model>` or `unassigned`. Locale-independent — safe as a map key,
   *  a React key and a sort input. */
  key: string;
  /** Display text for the target, already translated. */
  label: string;
}

/** The fields a target is derived from — anything item-shaped satisfies it. */
export type QueueTargetItem = Pick<
  PrintQueueItem,
  'printer_id' | 'printer_name' | 'target_model' | 'target_printer_ids'
>;

/** Printer id → display name. Either shape a caller already holds. */
export type PrinterNameSource = ReadonlyMap<number, string> | Record<number, string>;

/** Minimal translator contract (mirrors `utils/waitingReason.ts`). */
type Translate = (key: string) => string;

/**
 * The locale-independent half of a target — kind, key, and the narrowed field
 * the kind is built from. Both public functions read the precedence from here,
 * so there is exactly one place that decides what a unit targets.
 */
type TargetIdentity =
  | { kind: 'printer'; key: string; printerId: number }
  | { kind: 'printers'; key: string; printerIds: readonly number[] }
  | { kind: 'model'; key: string; model: string }
  | { kind: 'unassigned'; key: string };

function targetIdentity(item: QueueTargetItem): TargetIdentity {
  if (item.printer_id) {
    return { kind: 'printer', key: `printer:${item.printer_id}`, printerId: item.printer_id };
  }
  // Ids are used AS RECEIVED — the backend sends them sorted and deduped, so
  // re-sorting here would only risk two spellings of one pool.
  const pool = item.target_printer_ids;
  if (pool && pool.length > 0) {
    return { kind: 'printers', key: `printers:${pool.join(',')}`, printerIds: pool };
  }
  if (item.target_model) {
    return { kind: 'model', key: `model:${item.target_model}`, model: item.target_model };
  }
  return { kind: 'unassigned', key: 'unassigned' };
}

function printerName(source: PrinterNameSource, id: number): string | undefined {
  return source instanceof Map ? source.get(id) : (source as Record<number, string>)[id];
}

/**
 * Describe a unit's target: its kind, its stable grouping key, and its label.
 *
 * Display precedence — `printer_id`, then `target_printer_ids`, then
 * `target_model`, then unassigned (see the module comment for why this is the
 * landed-printer-first reading rather than the backend's).
 */
export function describeQueueTarget(
  item: QueueTargetItem,
  t: Translate,
  printerNameById: PrinterNameSource,
): QueueTarget {
  const identity = targetIdentity(item);
  const label = ((): string => {
    switch (identity.kind) {
      case 'printer':
        return item.printer_name || `${t('common.printer')} #${identity.printerId}`;
      case 'printers': {
        const names = identity.printerIds
          .map((id) => printerName(printerNameById, id) ?? `#${id}`)
          .join(', ');
        return `${t('queue.filter.anyOf')} ${names}`;
      }
      case 'model':
        return `${t('queue.filter.any')} ${identity.model}`;
      case 'unassigned':
        return t('queue.filter.unassigned');
    }
  })();
  return { kind: identity.kind, key: identity.key, label };
}

/**
 * Sort key grouping units by target IDENTITY, in the scheduler's own order:
 * the pool lanes first (each distinct pool its own group, by key), then
 * unassigned, then pinned printers in numeric id order.
 *
 * Its two readers are the SJF mirror (`queueSort.compareShortestFirst`, which
 * lanes by target because `print_scheduler.check_queue` does) and the
 * by-target buckets (`queueSort.bucketRowsByTarget`, whose lanes ARE targets).
 * It is NEVER the lane for the stored order — target identity is not the scope
 * a `position` is numbered within, and `queuePositionScopeKey` is. Laning the
 * stored order by identity displays an appended pool run above older ones and
 * then posts that display order back as `ordered_ids`.
 *
 * A string rather than a number so the key itself separates the pool lanes —
 * every distinct pool gets its own group, which the old arithmetic could not
 * express (a printers-pool unit collapsed onto the unassigned group, and two
 * models sharing a first letter shared one group). Built from ids only, never
 * from `label`, so the grouping does not reshuffle per locale.
 */
export function queueTargetSortKey(item: QueueTargetItem): string {
  const identity = targetIdentity(item);
  switch (identity.kind) {
    case 'printers':
    case 'model':
      return `0:${identity.key}`;
    case 'unassigned':
      return '1:unassigned';
    case 'printer':
      // Zero-padded so printer 2 sorts before printer 10.
      return `2:${String(identity.printerId).padStart(10, '0')}`;
  }
}

/** Kind → the classes every surface that colours a target reads. */
export interface QueueTargetKindClasses {
  /** Tint for a row chip's icon + label. A pinned printer is deliberately
   *  UNTINTED: on a row the printer name is the row's own fact and reads in
   *  the row's grey, while the accent colour is reserved for the two surfaces
   *  below, where the target IS the subject rather than one detail of a row. */
  chip: string;
  /** Tint for the by-target group header's icon. */
  icon: string;
  /** The by-target group's 3px left accent, running header + body. */
  rail: string;
}

/**
 * The ONE kind → colour table. `components/QueueTargetChip`, the by-target
 * group header icon and that group's left accent all read it, so a target
 * cannot be blue in one surface and grey in the next. It lives beside the kind
 * it keys on rather than in the chip, because three surfaces read it and only
 * one of them is that component.
 *
 * Class strings are literals because Tailwind scans source text — never
 * compose them. Each tinted kind carries a light/dark pair.
 */
const KIND_CLASSES: Record<QueueTargetKind, QueueTargetKindClasses> = {
  printer: {
    chip: '',
    icon: 'text-bambu-green',
    rail: 'border-l-bambu-green',
  },
  model: {
    chip: 'text-blue-700 dark:text-blue-400',
    icon: 'text-blue-700 dark:text-blue-400',
    rail: 'border-l-blue-700 dark:border-l-blue-400',
  },
  printers: {
    chip: 'text-blue-700 dark:text-blue-400',
    icon: 'text-blue-700 dark:text-blue-400',
    rail: 'border-l-blue-700 dark:border-l-blue-400',
  },
  unassigned: {
    chip: 'text-orange-700 dark:text-orange-400',
    icon: 'text-orange-700 dark:text-orange-400',
    rail: 'border-l-orange-700 dark:border-l-orange-400',
  },
};

export function queueTargetKindClasses(kind: QueueTargetKind): QueueTargetKindClasses {
  return KIND_CLASSES[kind];
}

/**
 * The SCOPE a unit's `position` is numbered within — the frontend mirror of
 * backend `queue_builder.position_scope_of`.
 *
 * There are exactly two shapes, and neither is a target: a PINNED row is
 * numbered against its own printer, and every NULL-`printer_id` row — model
 * pool, printers pool and unassigned alike — shares ONE sequence, because none
 * of them has a machine yet and the scheduler draws them from one list. Two
 * rows in different scopes can both hold position 1; two rows in the same
 * scope never can.
 *
 * The shared sequence sorts FIRST because the scheduler's `ORDER BY
 * printer_id, position` yields NULL-`printer_id` rows first on the farm's
 * SQLite store. Pinned scopes follow in numeric printer order, zero-padded so
 * printer 2 precedes printer 10.
 *
 * This is the ONLY correct lane for a stored-order comparator: the display
 * order of a stored-order list is what a drag posts back as `ordered_ids`, so
 * laning it by anything the backend does not number by rewrites the real
 * queue.
 */
export function queuePositionScopeKey(item: QueueTargetItem): string {
  const printerId = item.printer_id;
  if (printerId == null) return '0:shared';
  return `1:${String(printerId).padStart(10, '0')}`;
}
