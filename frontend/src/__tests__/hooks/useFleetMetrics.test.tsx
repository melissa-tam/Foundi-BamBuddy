/**
 * `hooks/useFleetMetrics` — the query keys, the polling contract, and the pure
 * range resolver.
 *
 * The keys are asserted against the live query CACHE rather than against the
 * exported factory alone: a factory nobody passes to `useQuery` is a comment.
 * The same reasoning covers `refetchInterval` and `keepPreviousData` — the
 * summary card's honesty surface ("Updated HH:MM", the stale warning) and the
 * timeframe switch both depend on options, not on data.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import {
  FLEET_MAX_RANGE_DAYS,
  FLEET_STATUS_POLL_MS,
  fleetMetricsKeys,
  resolveFleetRange,
  useFleetOverview,
  useFleetPrinterIntervals,
  useFleetStatus,
  type FleetRange,
} from '../../hooks/useFleetMetrics';
import type { FleetStatus } from '../../types/fleetMetrics';
import type { TimeframePreset, TimeframeState } from '../../utils/timeframe';
import { makeFleetStatus, makeFleetStatusFirstRun, FIXTURE_SITE_TODAY } from '../fixtures/fleetMetrics';

function harness() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

const RANGE: FleetRange = { dateFrom: '2026-09-15', dateTo: '2026-09-21' };

describe('query keys', () => {
  it('polls the live tile on the status key every 30 s', async () => {
    const { client, wrapper } = harness();
    const { result } = renderHook(() => useFleetStatus(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const entry = client.getQueryCache().find({ queryKey: ['fleet-metrics', 'status'] });
    expect(entry).toBeDefined();
    expect(entry!.options.refetchInterval).toBe(FLEET_STATUS_POLL_MS);
    expect(FLEET_STATUS_POLL_MS).toBe(30_000);
    // The background-tab default stays off: a hidden tab polling a classifier
    // sweep is work nobody is reading.
    expect(entry!.options.refetchIntervalInBackground).toBeUndefined();
  });

  it('exposes dataUpdatedAt unwrapped, for the "Updated HH:MM" footer', async () => {
    const { wrapper } = harness();
    const { result } = renderHook(() => useFleetStatus(), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.dataUpdatedAt).toBeGreaterThan(0);
    expect(result.current.data?.site_today).toBe(FIXTURE_SITE_TODAY);
  });

  it('keys the overview on the resolved dates', async () => {
    const { client, wrapper } = harness();
    const { result } = renderHook(() => useFleetOverview(RANGE), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(
      client.getQueryCache().find({ queryKey: ['fleet-metrics', 'overview', '2026-09-15', '2026-09-21'] }),
    ).toBeDefined();
    expect(fleetMetricsKeys.overview(RANGE)).toEqual([
      'fleet-metrics',
      'overview',
      '2026-09-15',
      '2026-09-21',
    ]);
  });

  it('does not run the overview until the range is resolved', () => {
    const { client, wrapper } = harness();
    renderHook(() => useFleetOverview(undefined), { wrapper });

    const entries = client.getQueryCache().getAll();
    expect(entries.every((entry) => entry.state.fetchStatus === 'idle')).toBe(true);
    expect(entries.every((entry) => entry.state.data === undefined)).toBe(true);
  });

  it('keeps the previous window on screen while a new range loads', async () => {
    const { wrapper } = harness();
    const { result, rerender } = renderHook(({ range }: { range: FleetRange }) => useFleetOverview(range), {
      wrapper,
      initialProps: { range: RANGE },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const first = result.current.data;

    rerender({ range: { dateFrom: '2026-08-01', dateTo: '2026-08-31' } });
    // The new key has no data yet, and placeholderData hands back the old
    // response rather than a blank card.
    expect(result.current.data).toBe(first);
    expect(result.current.isPlaceholderData).toBe(true);

    await waitFor(() => expect(result.current.isPlaceholderData).toBe(false));
  });

  it('leaves the drill-down un-fetched until a cell is actually opened', async () => {
    const { client, wrapper } = harness();
    const { rerender, result } = renderHook(
      ({ enabled }: { enabled: boolean }) => useFleetPrinterIntervals(3, RANGE, { enabled }),
      { wrapper, initialProps: { enabled: false } },
    );

    expect(result.current.fetchStatus).toBe('idle');
    expect(result.current.data).toBeUndefined();

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(
      client.getQueryCache().find({
        queryKey: ['fleet-metrics', 'intervals', 3, '2026-09-15', '2026-09-21'],
      }),
    ).toBeDefined();
  });
});

// ── resolveFleetRange ───────────────────────────────────────────────────────

const timeframe = (preset: TimeframePreset, dateFrom?: string, dateTo?: string): TimeframeState => ({
  preset,
  dateFrom,
  dateTo,
});

describe('resolveFleetRange', () => {
  const status = makeFleetStatus();

  it('answers undefined until /status has said what today is', () => {
    expect(resolveFleetRange(timeframe('last-30'), undefined)).toBeUndefined();
  });

  it('anchors every preset on the SITE today, not the browser today', () => {
    const cases: Array<[TimeframePreset, FleetRange]> = [
      ['today', { dateFrom: '2026-09-21', dateTo: '2026-09-21' }],
      ['this-week', { dateFrom: '2026-09-21', dateTo: '2026-09-21' }],
      ['this-month', { dateFrom: '2026-09-01', dateTo: '2026-09-21' }],
      ['last-7', { dateFrom: '2026-09-15', dateTo: '2026-09-21' }],
      ['last-30', { dateFrom: '2026-08-23', dateTo: '2026-09-21' }],
      ['last-90', { dateFrom: '2026-06-24', dateTo: '2026-09-21' }],
      ['this-year', { dateFrom: '2026-01-01', dateTo: '2026-09-21' }],
    ];
    for (const [preset, expected] of cases) {
      expect(resolveFleetRange(timeframe(preset), status)).toEqual(expected);
    }
  });

  it('resolves all time to the day before history_since', () => {
    // history_since 2026-08-02T11:30 — one day early, so no real history is clipped.
    expect(resolveFleetRange(timeframe('all-time'), status)).toEqual({
      dateFrom: '2026-08-01',
      dateTo: '2026-09-21',
    });
  });

  it('clamps all time to the last 366 days', () => {
    const ancient = makeFleetStatus();
    const clamped = resolveFleetRange(timeframe('all-time'), {
      ...ancient,
      history_since: '2019-01-05T00:00:00',
    });
    expect(clamped).toEqual({ dateFrom: '2025-09-21', dateTo: '2026-09-21' });
  });

  it('resolves all time to a single day when nothing has ever been recorded', () => {
    const first: FleetStatus = makeFleetStatusFirstRun();
    expect(resolveFleetRange(timeframe('all-time'), first)).toEqual({
      dateFrom: '2026-09-21',
      dateTo: '2026-09-21',
    });
  });

  it('clamps a custom range to the longest window the server answers', () => {
    const clamped = resolveFleetRange(timeframe('custom', '2024-01-01', '2026-09-21'), status);
    expect(clamped).toEqual({ dateFrom: '2025-09-21', dateTo: '2026-09-21' });
    expect(FLEET_MAX_RANGE_DAYS).toBe(366);
  });

  it('keeps a custom range that already fits', () => {
    expect(resolveFleetRange(timeframe('custom', '2026-09-01', '2026-09-10'), status)).toEqual({
      dateFrom: '2026-09-01',
      dateTo: '2026-09-10',
    });
  });

  it('asks for nothing when a custom range is incomplete or reversed', () => {
    expect(resolveFleetRange(timeframe('custom'), status)).toBeUndefined();
    expect(resolveFleetRange(timeframe('custom', '2026-09-10'), status)).toBeUndefined();
    expect(resolveFleetRange(timeframe('custom', '2026-09-10', '2026-09-01'), status)).toBeUndefined();
  });
});
