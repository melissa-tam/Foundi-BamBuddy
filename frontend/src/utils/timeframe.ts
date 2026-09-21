/**
 * Timeframe presets — THE owner of "which calendar range does this preset mean".
 *
 * Every page that offers the preset picker resolves its range here. The range
 * is computed by pure calendar arithmetic on an ANCHOR the caller passes in —
 * a `YYYY-MM-DD` calendar date — so the function never reads the clock and
 * never depends on the machine's timezone. That is what lets one preset list
 * serve two different definitions of "today":
 *
 *   - the Prints tab anchors on `utcToday()`. It reads archives by UTC day and
 *     an external accounting service pulls the SAME endpoint by UTC day, so its
 *     day boundary is not ours to move.
 *   - the Fleet tab anchors on the server's `site_today` (`utils/site_time.py`),
 *     because fleet history is bucketed on SITE days and the browser's zone is
 *     not the farm's.
 *
 * Reading the clock inside the resolver is what made those two impossible to
 * serve from one place, and made the resolver untestable without faking time.
 * The clock is read in exactly one function here (`utcToday`), and it takes an
 * injectable `now`.
 *
 * WEEK = ISO-8601, Monday-start — the same definition the backend's
 * `bucket_edges` uses, pinned on both sides by the shared vectors in
 * `backend/tests/_fixtures/site_week_vectors.json`.
 */

export type TimeframePreset =
  | 'today'
  | 'this-week'
  | 'this-month'
  | 'last-7'
  | 'last-30'
  | 'last-90'
  | 'this-year'
  | 'all-time'
  | 'custom';

/**
 * A resolved range. `undefined` on both ends means "unbounded" (`all-time`);
 * `custom` resolves to an empty object because the caller supplies the dates.
 */
export interface TimeframeRange {
  dateFrom?: string;
  dateTo?: string;
}

/** The picker's selection: a preset plus the dates a `custom` preset carries. */
export interface TimeframeState {
  preset: TimeframePreset;
  dateFrom: string | undefined; // YYYY-MM-DD
  dateTo: string | undefined; // YYYY-MM-DD
}

/** The presets offered in the picker, in display order. `custom` is a separate control. */
export const TIMEFRAME_PRESETS: readonly TimeframePreset[] = [
  'today',
  'this-week',
  'this-month',
  'last-7',
  'last-30',
  'last-90',
  'this-year',
  'all-time',
];

const CALENDAR_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

interface CalendarDate {
  year: number;
  /** 0-11, the `Date.UTC` convention. */
  month: number;
  day: number;
}

/**
 * Build a `YYYY-MM-DD` key from calendar parts, rolling out-of-range values
 * over the way `Date.UTC` does — `dayOfMonth - 29` in January lands in
 * December of the previous year, leap days included. UTC constructors and
 * getters only, so the result never depends on where the browser is.
 */
function calendarKey(year: number, month: number, day: number): string {
  return new Date(Date.UTC(year, month, day)).toISOString().slice(0, 10);
}

/**
 * Parse an anchor. Throws on anything that is not a real `YYYY-MM-DD` calendar
 * date, rather than letting a malformed one (a timestamp, an empty string, or
 * `2026-02-30`) silently produce a `NaN` range the page would send to the API.
 */
function parseAnchor(anchor: string): CalendarDate {
  const parts = CALENDAR_DATE.exec(anchor);
  if (!parts) {
    throw new RangeError(`timeframe: anchor must be a YYYY-MM-DD calendar date, got "${anchor}"`);
  }
  const year = Number(parts[1]);
  const month = Number(parts[2]) - 1;
  const day = Number(parts[3]);
  // Round-trip: rejects 2026-02-30, month 13, and the two-digit-year mapping
  // Date.UTC applies to years below 100.
  if (calendarKey(year, month, day) !== anchor) {
    throw new RangeError(`timeframe: anchor is not a real calendar date: "${anchor}"`);
  }
  return { year, month, day };
}

/**
 * The ISO-8601 week start (Monday) of a calendar date, as `YYYY-MM-DD`.
 * Pinned against the backend's week vectors.
 */
export function weekStart(date: string): string {
  const { year, month, day } = parseAnchor(date);
  const dayOfWeek = new Date(Date.UTC(year, month, day)).getUTCDay(); // 0 = Sunday
  return calendarKey(year, month, day - (dayOfWeek === 0 ? 6 : dayOfWeek - 1));
}

/**
 * Today as a UTC calendar date — the Prints tab's anchor, and the ONE place in
 * this module that reads a clock. `now` is injectable so callers and tests can
 * pin it.
 */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/**
 * Resolve a preset to an inclusive `date_from` / `date_to` pair, relative to
 * `anchor` (a `YYYY-MM-DD` calendar date — the caller's definition of "today").
 */
export function computeDateRange(preset: TimeframePreset, anchor: string): TimeframeRange {
  // Neither reads the anchor, so neither validates it: "all time" is unbounded
  // and a custom range carries its own dates.
  if (preset === 'all-time') return { dateFrom: undefined, dateTo: undefined };
  if (preset === 'custom') return {};

  const { year, month, day } = parseAnchor(anchor);
  const today = calendarKey(year, month, day);

  switch (preset) {
    case 'today':
      return { dateFrom: today, dateTo: today };
    case 'this-week':
      return { dateFrom: weekStart(anchor), dateTo: today };
    case 'this-month':
      return { dateFrom: calendarKey(year, month, 1), dateTo: today };
    case 'last-7':
      return { dateFrom: calendarKey(year, month, day - 6), dateTo: today };
    case 'last-30':
      return { dateFrom: calendarKey(year, month, day - 29), dateTo: today };
    case 'last-90':
      return { dateFrom: calendarKey(year, month, day - 89), dateTo: today };
    case 'this-year':
      return { dateFrom: calendarKey(year, 0, 1), dateTo: today };
    default:
      return preset satisfies never;
  }
}
