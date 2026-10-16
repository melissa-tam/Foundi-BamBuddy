/**
 * The touch view's paging math: columns x rows from the measured grid area (tiles never
 * under 300 px, 12 px gaps), the page count, and the swipe direction.
 */
import { describe, expect, it } from 'vitest';
import { clampPage, fitToCount, pageCount, pageSlice, swipeDirection, touchGrid } from '../../utils/touchGrid';

describe('touchGrid', () => {
  it('fits 4 x 2 on a 1280 x 720 screen (64 px bar, 12 px padding)', () => {
    // Grid area: 1280 - 24 wide, 720 - 64 - 24 high.
    expect(touchGrid(1256, 632)).toEqual({ cols: 4, rows: 2, perPage: 8 });
  });

  it('fits 6 x 3 on a 1920 x 1080 screen', () => {
    expect(touchGrid(1896, 992)).toEqual({ cols: 6, rows: 3, perPage: 18 });
  });

  it('never drops a tile under the 300 px floor', () => {
    // 611 px: two 300 px tiles need 612 with the gap.
    expect(touchGrid(611, 611).cols).toBe(1);
    expect(touchGrid(612, 612)).toEqual({ cols: 2, rows: 2, perPage: 4 });
  });

  it('shows one tile when nothing is measured yet', () => {
    expect(touchGrid(0, 0)).toEqual({ cols: 1, rows: 1, perPage: 1 });
    expect(touchGrid(Number.NaN, 200)).toEqual({ cols: 1, rows: 1, perPage: 1 });
  });
});

describe('fitToCount', () => {
  it('draws only the rows the tiles fill when they all fit on one page', () => {
    // 12 printers on a 6 x 3 screen: two full rows that grow into the height.
    expect(fitToCount({ cols: 6, rows: 3, perPage: 18 }, 12)).toEqual({ cols: 6, rows: 2, perPage: 12 });
    expect(fitToCount({ cols: 6, rows: 3, perPage: 18 }, 13)).toEqual({ cols: 6, rows: 3, perPage: 18 });
  });

  it('leaves a paged grid alone', () => {
    expect(fitToCount({ cols: 4, rows: 2, perPage: 8 }, 15)).toEqual({ cols: 4, rows: 2, perPage: 8 });
    expect(fitToCount({ cols: 4, rows: 2, perPage: 8 }, 0)).toEqual({ cols: 4, rows: 2, perPage: 8 });
  });
});

describe('pages', () => {
  it('counts pages, one even when empty', () => {
    expect(pageCount(15, 8)).toBe(2);
    expect(pageCount(16, 8)).toBe(2);
    expect(pageCount(17, 8)).toBe(3);
    expect(pageCount(0, 8)).toBe(1);
  });

  it('clamps a stored page into range', () => {
    expect(clampPage(5, 2)).toBe(1);
    expect(clampPage(-1, 2)).toBe(0);
    expect(clampPage(Number.NaN, 2)).toBe(0);
  });

  it('slices the page', () => {
    expect(pageSlice([1, 2, 3, 4, 5], 1, 2)).toEqual([3, 4]);
    expect(pageSlice([1, 2, 3, 4, 5], 2, 2)).toEqual([5]);
  });
});

describe('swipeDirection', () => {
  it('pages next on a left swipe and back on a right swipe', () => {
    expect(swipeDirection(-120, 10)).toBe('next');
    expect(swipeDirection(120, -10)).toBe('prev');
  });

  it('treats a tap or a mostly vertical drag as no page turn', () => {
    expect(swipeDirection(-20, 0)).toBeNull();
    expect(swipeDirection(-80, 140)).toBeNull();
  });
});
