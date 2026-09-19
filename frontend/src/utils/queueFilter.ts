/**
 * Queue filter predicates — THE owner of "does this unit match the filters".
 *
 * A pool unit has no printer of its own, so a location filter that only reads
 * `printer_id` hides every unit the farm has not placed yet: the exact rows an
 * operator opens the queue to find. Both predicates are pure so the page can
 * apply them to any tab without a second reading appearing per tab.
 */
import type { PrintQueueItem } from '../api/client';

/** The printer fields a location filter needs — anything fleet-shaped fits. */
export interface FilterPrinter {
  id: number;
  location?: string | null;
  model?: string | null;
}

/** The unit fields a location filter reads. */
export type LocationFilterItem = Pick<
  PrintQueueItem,
  'printer_id' | 'target_model' | 'target_printer_ids' | 'target_location'
>;

function sameModel(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false;
  return a.trim().toLowerCase() === b.trim().toLowerCase();
}

/**
 * Does this unit target the given location?
 *
 * Ladder, most specific first: the unit's OWN `target_location` (an operator's
 * explicit narrowing wins over any printer's address), then the pinned printer,
 * then any member of a printers pool, then any printer of the target model.
 * An unassigned unit targets nothing, so it never matches a location — it is
 * found through the unassigned lane, not through a place.
 *
 * An empty `location` means "no filter" and matches everything.
 */
export function matchesLocation(
  item: LocationFilterItem,
  location: string,
  printers: readonly FilterPrinter[],
): boolean {
  if (!location) return true;
  if (item.target_location) return item.target_location === location;
  if (item.printer_id != null) {
    return printers.some((p) => p.id === item.printer_id && p.location === location);
  }
  const pool = item.target_printer_ids;
  if (pool && pool.length > 0) {
    return pool.some((id) => printers.some((p) => p.id === id && p.location === location));
  }
  if (item.target_model) {
    return printers.some((p) => sameModel(p.model, item.target_model) && p.location === location);
  }
  return false;
}

/** Status filter. An empty `status` means "no filter". */
export function matchesStatus(
  item: Pick<PrintQueueItem, 'status'>,
  status: string,
): boolean {
  if (!status) return true;
  return item.status === status;
}
