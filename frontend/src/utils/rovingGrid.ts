/**
 * Roving-focus movement over a grid whose column set is RESPONSIVE.
 *
 * A WAI-ARIA grid is one tab stop with a moving caret, and the naive
 * implementation of "move right" is `activeCol + 1`. That is correct only when
 * every column is on screen. The fleet matrix's is not: the bucket columns are
 * `display:none` until "Show days" on a phone, and the Time-split bar only
 * exists below the container breakpoint — so `+ 1` walks the caret into a
 * column that has no box, focus refuses to land, and the caret sits still. The
 * observed symptom was arrowing right from Avg going 0→1→2→2→2 for ever.
 *
 * The fix is to move over the VISIBLE columns, and the honest way to know which
 * those are is to ask the browser at key time rather than to re-derive the
 * breakpoints in JS — a second definition of the layout that would be wrong
 * exactly when the sidebar is collapsed. This module owns the arithmetic; the
 * component owns the asking.
 */

/**
 * Step `delta` places through `visible`, clamped at both ends.
 *
 * `current` need not be in the list. A disclosure can close under the caret
 * (pressing "Hide days" while a day column is focused), and the caret then
 * lands on the nearest surviving column in the direction it was heading —
 * rescuing it IS the move, so one press lands on one real column instead of
 * stepping off a position that no longer exists and skipping a column.
 */
export function stepVisibleColumn(
  visible: readonly number[],
  current: number,
  delta: number,
): number {
  if (visible.length === 0) return current;

  const index = visible.indexOf(current);
  if (index === -1) {
    // The caret's own column has gone. RESCUING it is the whole move: one key
    // press puts the caret on the nearest surviving column in the direction it
    // was heading, rather than stepping off a position that no longer exists
    // and skipping a column on the way.
    if (delta >= 0) {
      const after = visible.find((col) => col > current);
      return after ?? visible[visible.length - 1] ?? current;
    }
    let before: number | undefined;
    for (const col of visible) {
      if (col < current) before = col;
    }
    return before ?? visible[0] ?? current;
  }

  const next = Math.min(visible.length - 1, Math.max(0, index + delta));
  return visible[next] ?? current;
}

/**
 * Is this element actually LAID OUT?
 *
 * `checkVisibility` is the browser's own answer and covers every way the matrix
 * hides a column — `display:none`, a zero-width collapse, `content-visibility`
 * — without this module knowing which one was used.
 *
 * Under jsdom no stylesheet is loaded, so nothing is ever hidden and every
 * column is navigable. That is deliberate: it is what the RTL grid tests
 * describe, and the responsive behaviour they cannot see is pinned by
 * `stepVisibleColumn` directly and verified in a real browser.
 */
export function elementIsLaidOut(element: HTMLElement): boolean {
  if (typeof element.checkVisibility !== 'function') return true;
  return element.checkVisibility({ visibilityProperty: true });
}
