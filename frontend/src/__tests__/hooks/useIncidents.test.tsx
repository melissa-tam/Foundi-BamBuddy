/**
 * `hooks/useIncidents` — the query key, freshness, keep-previous-data and the
 * disabled-until-resolved contract, asserted against the live query CACHE (a
 * key factory nobody passes to `useQuery` is a comment).
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { FLEET_OVERVIEW_STALE_MS } from '../../hooks/useFleetMetrics';
import { incidentsKeys, useIncidents } from '../../hooks/useIncidents';
import type { IncidentsQuery } from '../../types/incidents';
import { makeIncidentsResponse } from '../fixtures/incidents';
import { server } from '../mocks/server';

function harness() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

const QUERY: IncidentsQuery = { dateFrom: '2026-09-01', dateTo: '2026-09-21', limit: 50, offset: 0 };

describe('useIncidents', () => {
  it('keys the page on window, filters, limit and offset', async () => {
    const { client, wrapper } = harness();
    const query: IncidentsQuery = { ...QUERY, kind: 'jam', outcome: 'held', printerId: 3, offset: 50 };
    const { result } = renderHook(() => useIncidents(query), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const key = ['incidents', 'list', '2026-09-01', '2026-09-21', 'jam', 'held', 3, 50, 50];
    expect(incidentsKeys.list(query)).toEqual(key);
    const entry = client.getQueryCache().find({ queryKey: key });
    expect(entry).toBeDefined();
    expect((entry!.options as { staleTime?: number }).staleTime).toBe(FLEET_OVERVIEW_STALE_MS);
  });

  it('sends the window, filters and page as query parameters', async () => {
    let seen: URLSearchParams | null = null;
    server.use(
      http.get('/api/v1/incidents', ({ request }) => {
        seen = new URL(request.url).searchParams;
        return HttpResponse.json(makeIncidentsResponse());
      }),
    );
    const { wrapper } = harness();
    const { result } = renderHook(
      () => useIncidents({ ...QUERY, outcome: 'auto_recovered', printerId: 7, offset: 100 }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const params = seen as URLSearchParams | null;
    expect(params?.get('date_from')).toBe('2026-09-01');
    expect(params?.get('date_to')).toBe('2026-09-21');
    expect(params?.get('outcome')).toBe('auto_recovered');
    expect(params?.get('printer_id')).toBe('7');
    expect(params?.get('limit')).toBe('50');
    expect(params?.get('offset')).toBe('100');
    expect(params?.has('kind')).toBe(false);
  });

  it('does not run until the window is resolved', () => {
    const { client, wrapper } = harness();
    renderHook(() => useIncidents(undefined), { wrapper });

    const entries = client.getQueryCache().getAll();
    expect(entries.every((entry) => entry.state.fetchStatus === 'idle')).toBe(true);
    expect(entries.every((entry) => entry.state.data === undefined)).toBe(true);
  });

  it('keeps the previous page on screen while the next one loads', async () => {
    const { wrapper } = harness();
    const { result, rerender } = renderHook(({ query }: { query: IncidentsQuery }) => useIncidents(query), {
      wrapper,
      initialProps: { query: QUERY },
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const first = result.current.data;

    rerender({ query: { ...QUERY, offset: 50 } });
    expect(result.current.data).toBe(first);
    expect(result.current.isPlaceholderData).toBe(true);

    await waitFor(() => expect(result.current.isPlaceholderData).toBe(false));
  });
});
