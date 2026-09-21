/**
 * The fleet matrix's roving caret, over a column set the layout keeps changing.
 *
 * This is the arithmetic half of the fix for a caret that got STUCK: the naive
 * `activeCol ± 1` walked into a column the responsive layout had removed, focus
 * refused to land there, and the caret stopped moving — arrowing right from Avg
 * went 0 → 1 → 2 → 2 → 2 for ever. jsdom has no layout, so the browser half
 * (which column is actually on screen) cannot be tested here and is verified in
 * a real browser; what IS testable is that movement over a list with holes in
 * it behaves, and that is what this file pins.
 */
import { describe, expect, it } from 'vitest';
import { elementIsLaidOut, stepVisibleColumn } from '../../utils/rovingGrid';

/** Printer · Total · Avg, then the day columns — with the days hidden. */
const FROZEN_ONLY = [0, 1, 2];
/** The same grid with "Show days" pressed. */
const WITH_DAYS = [0, 1, 2, 3, 4, 5, 6];

describe('stepVisibleColumn', () => {
  it('moves one visible column at a time', () => {
    expect(stepVisibleColumn(WITH_DAYS, 0, 1)).toBe(1);
    expect(stepVisibleColumn(WITH_DAYS, 2, 1)).toBe(3);
    expect(stepVisibleColumn(WITH_DAYS, 3, -1)).toBe(2);
  });

  it('clamps at both ends instead of falling off the grid', () => {
    expect(stepVisibleColumn(WITH_DAYS, 0, -1)).toBe(0);
    expect(stepVisibleColumn(WITH_DAYS, 6, 1)).toBe(6);
  });

  it('stops at the last VISIBLE column, not the last column that exists', () => {
    // The regression this whole module exists for: with the days hidden, the
    // caret must come to rest on Avg rather than trying for a day column that
    // has no box — and it must come to rest there, not sit refusing to move
    // while the reader presses the key again.
    expect(stepVisibleColumn(FROZEN_ONLY, 2, 1)).toBe(2);
    expect(stepVisibleColumn(FROZEN_ONLY, 1, 1)).toBe(2);
  });

  it('skips a hole in the middle of the column set', () => {
    // Column 3 is hidden; right from Avg reaches the first bucket in ONE press.
    const withHole = [0, 1, 2, 4, 5];
    expect(stepVisibleColumn(withHole, 2, 1)).toBe(4);
    expect(stepVisibleColumn(withHole, 4, -1)).toBe(2);
  });

  it('pages by ten visible columns, clamped', () => {
    const many = Array.from({ length: 30 }, (_, index) => index);
    expect(stepVisibleColumn(many, 3, 10)).toBe(13);
    expect(stepVisibleColumn(many, 13, -10)).toBe(3);
    expect(stepVisibleColumn(many, 25, 10)).toBe(29);
    expect(stepVisibleColumn(many, 4, -10)).toBe(0);
  });

  it('rescues a caret whose own column has just been hidden', () => {
    // "Hide days" pressed while a day column held the caret: the next press
    // must land somewhere real rather than leaving focus nowhere.
    expect(stepVisibleColumn(FROZEN_ONLY, 5, -1)).toBe(2);
    expect(stepVisibleColumn(FROZEN_ONLY, 5, 1)).toBe(2);
    const withHole = [0, 1, 2, 6];
    expect(stepVisibleColumn(withHole, 4, 1)).toBe(6);
    expect(stepVisibleColumn(withHole, 4, -1)).toBe(2);
  });

  it('answers with the current column when nothing is visible at all', () => {
    expect(stepVisibleColumn([], 3, 1)).toBe(3);
  });
});

describe('elementIsLaidOut', () => {
  it('treats an element as laid out when the engine cannot say otherwise', () => {
    // jsdom loads no stylesheet, so nothing is ever hidden and every column is
    // navigable — which is exactly what the RTL grid tests describe.
    const element = document.createElement('th');
    expect(elementIsLaidOut(element)).toBe(true);
  });

  it('asks the browser rather than re-deriving the breakpoint', () => {
    const element = document.createElement('th');
    const asked: unknown[] = [];
    (element as unknown as { checkVisibility: (o?: unknown) => boolean }).checkVisibility = (
      options,
    ) => {
      asked.push(options);
      return false;
    };

    expect(elementIsLaidOut(element)).toBe(false);
    // …and it asks about `visibility` too, because the matrix hides one column
    // by collapsing it rather than by removing it.
    expect(asked).toEqual([{ visibilityProperty: true }]);
  });
});
