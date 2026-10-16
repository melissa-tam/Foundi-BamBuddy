/**
 * The touch view's paging math: how many tiles fit the measured grid area with no
 * page scroll, how many pages that makes, and which way a swipe pages. Pure; the page
 * measures its grid (ResizeObserver) and feeds the numbers in.
 */

/** A tile is never smaller than this, in CSS px, on either axis. */
export const TOUCH_TILE_MIN_PX = 300;
/** The gap between tiles, in CSS px. */
export const TOUCH_GAP_PX = 12;
/** A horizontal drag shorter than this (CSS px) is a tap, never a page turn. */
export const SWIPE_THRESHOLD_PX = 60;

export interface TouchGrid {
  cols: number;
  rows: number;
  /** Tiles per page: `cols * rows`, at least 1. */
  perPage: number;
}

/** How many tiles of at least `minTile` fit in `size` px with `gap` px between them. */
function fit(size: number, minTile: number, gap: number): number {
  if (!Number.isFinite(size) || size <= 0) return 1;
  return Math.max(1, Math.floor((size + gap) / (minTile + gap)));
}

/** Columns x rows for a grid area of `width` x `height` CSS px. */
export function touchGrid(
  width: number,
  height: number,
  minTile: number = TOUCH_TILE_MIN_PX,
  gap: number = TOUCH_GAP_PX,
): TouchGrid {
  const cols = fit(width, minTile, gap);
  const rows = fit(height, minTile, gap);
  return { cols, rows, perPage: cols * rows };
}

/** Pages needed for `total` tiles; one page even when there is nothing to show. */
export function pageCount(total: number, perPage: number): number {
  return Math.max(1, Math.ceil(total / Math.max(1, perPage)));
}

/** A page index kept inside `[0, pages - 1]`. */
export function clampPage(page: number, pages: number): number {
  if (!Number.isFinite(page)) return 0;
  return Math.min(Math.max(0, Math.trunc(page)), Math.max(0, pages - 1));
}

/** The items on `page` (0-based). */
export function pageSlice<T>(items: readonly T[], page: number, perPage: number): T[] {
  const start = page * perPage;
  return items.slice(start, start + perPage);
}

export type SwipeDirection = 'next' | 'prev';

/**
 * Which page a pointer drag of `dx` x `dy` px turns to: left = next, right = previous;
 * null for a tap or a mostly vertical drag.
 */
export function swipeDirection(
  dx: number,
  dy: number,
  threshold: number = SWIPE_THRESHOLD_PX,
): SwipeDirection | null {
  if (Math.abs(dx) < threshold || Math.abs(dx) <= Math.abs(dy)) return null;
  return dx < 0 ? 'next' : 'prev';
}

/**
 * The grid the page draws: when every tile fits on one page with rows to spare, only the
 * rows the tiles fill, so they grow into the height instead of leaving it empty. Paging
 * reads `perPage` of the fitted grid (unchanged when more than one page is needed).
 */
export function fitToCount(grid: TouchGrid, total: number): TouchGrid {
  if (total >= grid.perPage || total <= 0) return grid;
  const rows = Math.max(1, Math.ceil(total / grid.cols));
  return { cols: grid.cols, rows, perPage: grid.cols * rows };
}
