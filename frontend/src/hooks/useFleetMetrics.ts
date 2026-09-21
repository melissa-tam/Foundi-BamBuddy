/**
 * THE fleet-metrics query module — the only place the `['fleet-metrics', …]`
 * keys are spelled, and the only place the three endpoints are called.
 *
 * Three reads with deliberately different appetites, which is exactly why they
 * are three queries and not one:
 *
 * - **`/status` is the live tile.** It polls every 30 s and never waits on
 *   history, so "is anything down right now" renders the moment the page opens
 *   even while a 366-day window is still being swept. A silently stale "0 down"
 *   is the most dangerous thing this tab could show on a lights-out farm, so
 *   the card reads `dataUpdatedAt` off the query result and warns on age —
 *   which is why these hooks return the query result UNWRAPPED. A bespoke
 *   `{ data, loading }` shape would throw away the two fields the honesty
 *   surface is built from.
 * - **`/overview` is the window.** Keyed on the resolved dates, `staleTime`
 *   60 s, and `keepPreviousData` so changing the timeframe re-renders the old
 *   numbers dimmed instead of blanking the page.
 * - **`/intervals` is the drill-down.** Fetched only when a cell is opened.
 *
 * `resolveFleetRange` lives here rather than in `utils/fleetMetrics.ts` because
 * it is what turns the picker plus `/status` into this module's query key: it
 * is the argument, not the encoding.
 */

import {
  keepPreviousData,
  useQuery,
  type UseQueryResult,
} from '@tanstack/react-query';
import { api } from '../api/client';
import type { FleetOverview, FleetStatus, PrinterIntervalsResponse } from '../types/fleetMetrics';
import { addCalendarDays, computeDateRange, daysInclusive, type TimeframeState } from '../utils/timeframe';

/** The live tile's poll period. */
export const FLEET_STATUS_POLL_MS = 30_000;

/** How long a window's history stays fresh. A day's numbers do not move quickly. */
export const FLEET_OVERVIEW_STALE_MS = 60_000;

/**
 * The longest window the server will answer (it 422s above this), so the client
 * clamps first rather than sending a request it knows will fail.
 */
export const FLEET_MAX_RANGE_DAYS = 366;

/** A resolved, server-ready window. Inclusive SITE dates. */
export interface FleetRange {
  dateFrom: string;
  dateTo: string;
}

/**
 * The query keys, in one place so an invalidation can name them without
 * re-typing a string that only this module is supposed to know.
 */
export const fleetMetricsKeys = {
  all: ['fleet-metrics'] as const,
  status: () => ['fleet-metrics', 'status'] as const,
  overview: (range: FleetRange) => ['fleet-metrics', 'overview', range.dateFrom, range.dateTo] as const,
  intervals: (printerId: number, range: FleetRange) =>
    ['fleet-metrics', 'intervals', printerId, range.dateFrom, range.dateTo] as const,
};

/**
 * The live fleet tile.
 *
 * `refetchIntervalInBackground` is left at its default (off): a hidden tab
 * polling a classifier sweep every 30 s is work nobody is reading, and the
 * query refetches on focus anyway.
 */
export function useFleetStatus(): UseQueryResult<FleetStatus, Error> {
  return useQuery<FleetStatus, Error>({
    queryKey: fleetMetricsKeys.status(),
    queryFn: () => api.getFleetStatus(),
    refetchInterval: FLEET_STATUS_POLL_MS,
  });
}

/**
 * One window's history. `range` is `undefined` until `/status` has answered
 * (the Fleet tab's dates are anchored on the SITE's today, which only the
 * server knows), and the query simply does not run until then.
 */
export function useFleetOverview(range: FleetRange | undefined): UseQueryResult<FleetOverview, Error> {
  return useQuery<FleetOverview, Error>({
    // Safe: `enabled` gates the call, and the key is only read when it runs.
    queryKey: fleetMetricsKeys.overview(range ?? { dateFrom: '', dateTo: '' }),
    queryFn: () => api.getFleetOverview({ dateFrom: range!.dateFrom, dateTo: range!.dateTo }),
    enabled: range !== undefined,
    staleTime: FLEET_OVERVIEW_STALE_MS,
    placeholderData: keepPreviousData,
  });
}

export interface UseFleetPrinterIntervalsOptions {
  /** The drill-down is on demand: false until a cell is actually opened. */
  enabled?: boolean;
}

/** One printer's classified intervals — what a matrix cell was summed from. */
export function useFleetPrinterIntervals(
  printerId: number,
  range: FleetRange | undefined,
  { enabled = false }: UseFleetPrinterIntervalsOptions = {},
): UseQueryResult<PrinterIntervalsResponse, Error> {
  return useQuery<PrinterIntervalsResponse, Error>({
    queryKey: fleetMetricsKeys.intervals(printerId, range ?? { dateFrom: '', dateTo: '' }),
    queryFn: () =>
      api.getFleetPrinterIntervals(printerId, { dateFrom: range!.dateFrom, dateTo: range!.dateTo }),
    enabled: enabled && range !== undefined && Number.isFinite(printerId),
    staleTime: FLEET_OVERVIEW_STALE_MS,
  });
}

/** Clamp an inclusive range to at most `FLEET_MAX_RANGE_DAYS`, keeping its END. */
function clampToMaxDays(range: FleetRange): FleetRange {
  const span = daysInclusive(range.dateFrom, range.dateTo);
  if (span <= FLEET_MAX_RANGE_DAYS) return range;
  return {
    dateFrom: addCalendarDays(range.dateTo, -(FLEET_MAX_RANGE_DAYS - 1)),
    dateTo: range.dateTo,
  };
}

/**
 * Resolve the picker's selection into the window the Fleet tab asks for.
 *
 * Pure, and `undefined` until `/status` has answered — the Fleet tab's "today"
 * is the SITE's, and anchoring on the browser's would give a farm PC in another
 * zone a different last day from the one the server buckets on.
 *
 * **"All time" resolves to `history_since`,** which is the earlier of the first
 * observation and the first incident: fault and hold history reaches back to
 * 2026-08 while state history starts the day the recorder shipped, and a range
 * that stopped at the recorder would silently drop the older half.
 *
 * The server sends that as an INSTANT (naive UTC) and this module has no
 * timezone database, so the conversion to a site date is stated once here: take
 * the instant's UTC calendar date and step back one day. The error is bounded
 * at a day and it always errs EARLY, so the window can never clip off real
 * history; the extra leading day comes back as a `not_recorded` bucket, which
 * the matrix already renders as a dash.
 */
export function resolveFleetRange(
  timeframe: TimeframeState,
  status: FleetStatus | undefined,
): FleetRange | undefined {
  if (status === undefined) return undefined;
  const today = status.site_today;

  if (timeframe.preset === 'all-time') {
    if (status.history_since === null) return { dateFrom: today, dateTo: today };
    const firstDay = addCalendarDays(status.history_since.slice(0, 10), -1);
    const floor = addCalendarDays(today, -(FLEET_MAX_RANGE_DAYS - 1));
    return { dateFrom: firstDay < floor ? floor : firstDay, dateTo: today };
  }

  if (timeframe.preset === 'custom') {
    if (!timeframe.dateFrom || !timeframe.dateTo) return undefined;
    if (timeframe.dateTo < timeframe.dateFrom) return undefined;
    return clampToMaxDays({ dateFrom: timeframe.dateFrom, dateTo: timeframe.dateTo });
  }

  const { dateFrom, dateTo } = computeDateRange(timeframe.preset, today);
  if (dateFrom === undefined || dateTo === undefined) return undefined;
  return clampToMaxDays({ dateFrom, dateTo });
}

/** The label leaf for the resolved period, for the summary card's column head. */
export const TIMEFRAME_PERIOD_LABEL_KEY: Record<TimeframeState['preset'], string> = {
  today: 'fleetMetrics.summary.period.today',
  'this-week': 'fleetMetrics.summary.period.thisWeek',
  'this-month': 'fleetMetrics.summary.period.thisMonth',
  'last-7': 'fleetMetrics.summary.period.last7',
  'last-30': 'fleetMetrics.summary.period.last30',
  'last-90': 'fleetMetrics.summary.period.last90',
  'this-year': 'fleetMetrics.summary.period.thisYear',
  'all-time': 'fleetMetrics.summary.period.allTime',
  custom: 'fleetMetrics.summary.period.custom',
};
