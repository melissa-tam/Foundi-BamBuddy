/**
 * Timeframe resolver.
 *
 * The preset rows below are a CHARACTERIZATION of the resolver that lived in
 * `StatsPage.computeDateRange` before the extraction: they were recorded by
 * running the old function under `vi.setSystemTime(<clock>)` and are asserted
 * here against the new function anchored on that clock's UTC date. The Prints
 * tab reads archives by UTC day and an external accounting service pulls the
 * same endpoint by UTC day, so any drift in these values is a contract break,
 * not a refactor.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  TIMEFRAME_PRESETS,
  computeDateRange,
  utcToday,
  weekStart,
  type TimeframePreset,
  type TimeframeRange,
} from '../../utils/timeframe';

/**
 * anchor -> preset -> [dateFrom, dateTo], `null` meaning `undefined`.
 * Recorded from the pre-extraction implementation.
 */
const LEGACY: ReadonlyArray<readonly [string, ReadonlyArray<readonly [TimeframePreset, string | null, string | null]>]> = [
  ['2026-09-21', [
    ['today', '2026-09-21', '2026-09-21'],
    ['this-week', '2026-09-21', '2026-09-21'],
    ['this-month', '2026-09-01', '2026-09-21'],
    ['last-7', '2026-09-15', '2026-09-21'],
    ['last-30', '2026-08-23', '2026-09-21'],
    ['last-90', '2026-06-24', '2026-09-21'],
    ['this-year', '2026-01-01', '2026-09-21'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // Year boundary.
  ['2026-01-01', [
    ['today', '2026-01-01', '2026-01-01'],
    ['this-week', '2025-12-29', '2026-01-01'],
    ['this-month', '2026-01-01', '2026-01-01'],
    ['last-7', '2025-12-26', '2026-01-01'],
    ['last-30', '2025-12-03', '2026-01-01'],
    ['last-90', '2025-10-04', '2026-01-01'],
    ['this-year', '2026-01-01', '2026-01-01'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // Month boundary that also falls on a Sunday (the `day === 0` week branch).
  ['2026-03-01', [
    ['today', '2026-03-01', '2026-03-01'],
    ['this-week', '2026-02-23', '2026-03-01'],
    ['this-month', '2026-03-01', '2026-03-01'],
    ['last-7', '2026-02-23', '2026-03-01'],
    ['last-30', '2026-01-31', '2026-03-01'],
    ['last-90', '2025-12-02', '2026-03-01'],
    ['this-year', '2026-01-01', '2026-03-01'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // Leap day.
  ['2024-02-29', [
    ['today', '2024-02-29', '2024-02-29'],
    ['this-week', '2024-02-26', '2024-02-29'],
    ['this-month', '2024-02-01', '2024-02-29'],
    ['last-7', '2024-02-23', '2024-02-29'],
    ['last-30', '2024-01-31', '2024-02-29'],
    ['last-90', '2023-12-02', '2024-02-29'],
    ['this-year', '2024-01-01', '2024-02-29'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // The day after a leap day — every backward window crosses Feb 29.
  ['2024-03-01', [
    ['today', '2024-03-01', '2024-03-01'],
    ['this-week', '2024-02-26', '2024-03-01'],
    ['this-month', '2024-03-01', '2024-03-01'],
    ['last-7', '2024-02-24', '2024-03-01'],
    ['last-30', '2024-02-01', '2024-03-01'],
    ['last-90', '2023-12-03', '2024-03-01'],
    ['this-year', '2024-01-01', '2024-03-01'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // Last day of the year, in an ISO week that belongs to the next year.
  ['2026-12-31', [
    ['today', '2026-12-31', '2026-12-31'],
    ['this-week', '2026-12-28', '2026-12-31'],
    ['this-month', '2026-12-01', '2026-12-31'],
    ['last-7', '2026-12-25', '2026-12-31'],
    ['last-30', '2026-12-02', '2026-12-31'],
    ['last-90', '2026-10-03', '2026-12-31'],
    ['this-year', '2026-01-01', '2026-12-31'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // Month boundary falling on a Monday — "this week" starts on the anchor.
  ['2026-08-31', [
    ['today', '2026-08-31', '2026-08-31'],
    ['this-week', '2026-08-31', '2026-08-31'],
    ['this-month', '2026-08-01', '2026-08-31'],
    ['last-7', '2026-08-25', '2026-08-31'],
    ['last-30', '2026-08-02', '2026-08-31'],
    ['last-90', '2026-06-03', '2026-08-31'],
    ['this-year', '2026-01-01', '2026-08-31'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
  // A Sunday mid-month: the week reaches back into the previous month.
  ['2026-05-03', [
    ['today', '2026-05-03', '2026-05-03'],
    ['this-week', '2026-04-27', '2026-05-03'],
    ['this-month', '2026-05-01', '2026-05-03'],
    ['last-7', '2026-04-27', '2026-05-03'],
    ['last-30', '2026-04-04', '2026-05-03'],
    ['last-90', '2026-02-03', '2026-05-03'],
    ['this-year', '2026-01-01', '2026-05-03'],
    ['all-time', null, null],
    ['custom', null, null],
  ]],
];

const asRange = (from: string | null, to: string | null): TimeframeRange => ({
  dateFrom: from ?? undefined,
  dateTo: to ?? undefined,
});

describe('computeDateRange', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  for (const [anchor, rows] of LEGACY) {
    describe(`anchored on ${anchor}`, () => {
      for (const [preset, from, to] of rows) {
        it(`resolves ${preset} exactly as the pre-extraction page did`, () => {
          expect(computeDateRange(preset, anchor)).toEqual(asRange(from, to));
        });
      }
    });
  }

  it('covers every offered preset in the table', () => {
    const tabled = new Set(LEGACY[0][1].map(([preset]) => preset));
    for (const preset of TIMEFRAME_PRESETS) {
      expect(tabled.has(preset)).toBe(true);
    }
  });

  it('keeps "all time" unbounded and leaves a custom range to the caller', () => {
    expect(computeDateRange('all-time', '2026-09-21')).toEqual({
      dateFrom: undefined,
      dateTo: undefined,
    });
    expect(computeDateRange('custom', '2026-09-21')).toEqual({});
  });

  it('resolves the unanchored presets without reading the anchor at all', () => {
    // A caller that has no anchor yet (the Fleet tab before /status answers)
    // can still resolve these two.
    expect(computeDateRange('all-time', 'not-a-date')).toEqual({
      dateFrom: undefined,
      dateTo: undefined,
    });
    expect(computeDateRange('custom', '')).toEqual({});
  });

  it('rejects an anchor that is not a real calendar date', () => {
    expect(() => computeDateRange('today', '2026-02-30')).toThrow(RangeError);
    expect(() => computeDateRange('today', '2026-13-01')).toThrow(RangeError);
    expect(() => computeDateRange('today', '2026-09-21T00:00:00Z')).toThrow(RangeError);
    expect(() => computeDateRange('today', '')).toThrow(RangeError);
  });

  it('answers the same for one anchor whatever the machine zone is', () => {
    const original = process.env.TZ;
    const seen: TimeframeRange[][] = [];
    for (const tz of ['UTC', 'America/Los_Angeles', 'Pacific/Kiritimati', 'Asia/Kathmandu']) {
      process.env.TZ = tz;
      seen.push(TIMEFRAME_PRESETS.map((preset) => computeDateRange(preset, '2026-03-01')));
    }
    process.env.TZ = original;
    for (const row of seen) {
      expect(row).toEqual(seen[0]);
    }
  });

  it('answers the same for one anchor whatever the system clock says', () => {
    const atRealTime = TIMEFRAME_PRESETS.map((preset) => computeDateRange(preset, '2026-03-01'));

    vi.useFakeTimers();
    vi.setSystemTime(new Date('1999-07-04T05:06:07Z'));
    const atFakePast = TIMEFRAME_PRESETS.map((preset) => computeDateRange(preset, '2026-03-01'));
    vi.setSystemTime(new Date('2099-11-30T23:59:59Z'));
    const atFakeFuture = TIMEFRAME_PRESETS.map((preset) => computeDateRange(preset, '2026-03-01'));

    expect(atFakePast).toEqual(atRealTime);
    expect(atFakeFuture).toEqual(atRealTime);
  });
});

describe('weekStart', () => {
  /**
   * The SAME file the backend's `site_time.bucket_edges` tests are pinned to —
   * read, not copied, so a change to the week definition breaks both sides or
   * neither.
   */
  const vectorsPath = path.resolve(
    path.dirname(fileURLToPath(import.meta.url)),
    '../../../../backend/tests/_fixtures/site_week_vectors.json',
  );

  interface WeekVector {
    date: string;
    week_start: string;
  }

  it('reads the shared vectors the backend is pinned to', () => {
    expect(fs.existsSync(vectorsPath)).toBe(true);
  });

  it('agrees with every shared week vector', () => {
    const vectors: WeekVector[] = JSON.parse(fs.readFileSync(vectorsPath, 'utf8'));
    expect(vectors.length).toBeGreaterThan(0);
    for (const { date, week_start } of vectors) {
      expect(`${date} -> ${weekStart(date)}`).toBe(`${date} -> ${week_start}`);
    }
  });

  it('returns the date itself for a Monday and reaches back six days from a Sunday', () => {
    expect(weekStart('2026-09-21')).toBe('2026-09-21'); // Monday
    expect(weekStart('2026-09-27')).toBe('2026-09-21'); // Sunday
  });

  it('is the definition the "this week" preset resolves to', () => {
    for (const [anchor, rows] of LEGACY) {
      const thisWeek = rows.find(([preset]) => preset === 'this-week');
      expect(weekStart(anchor)).toBe(thisWeek?.[1]);
    }
  });
});

describe('utcToday', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads the UTC date either side of midnight', () => {
    expect(utcToday(new Date('2026-09-21T23:30:00Z'))).toBe('2026-09-21');
    expect(utcToday(new Date('2026-09-22T00:30:00Z'))).toBe('2026-09-22');
  });

  it('stays on the UTC day when the machine zone is hours away from it', () => {
    const original = process.env.TZ;
    // 23:30 UTC is already the next day in Kiritimati (+14) and still the
    // previous afternoon in Los Angeles (-7).
    for (const tz of ['Pacific/Kiritimati', 'America/Los_Angeles']) {
      process.env.TZ = tz;
      expect(utcToday(new Date('2026-09-21T23:30:00Z'))).toBe('2026-09-21');
      expect(utcToday(new Date('2026-09-22T00:30:00Z'))).toBe('2026-09-22');
    }
    process.env.TZ = original;
  });

  it('defaults to the system clock', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-30T12:00:00Z'));
    expect(utcToday()).toBe('2026-06-30');
  });

  it('produces an anchor computeDateRange accepts', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-21T23:30:00Z'));
    expect(computeDateRange('today', utcToday())).toEqual({
      dateFrom: '2026-09-21',
      dateTo: '2026-09-21',
    });
  });
});
