/**
 * THE fault-ledger query module — the only place the `['incidents', …]` keys
 * are spelled and the only caller of `api.getIncidents`.
 *
 * Same appetite as the Fleet tab's `/overview`: keyed on the resolved window
 * plus the tab's filters and page, fresh for `FLEET_OVERVIEW_STALE_MS`, and
 * `keepPreviousData` so paging or re-filtering redraws the old rows instead of
 * blanking the table. Returned UNWRAPPED: the table reads `dataUpdatedAt` to
 * advance an open row's held time between refetches.
 */

import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';
import { api } from '../api/client';
import type { IncidentsQuery, IncidentsResponse } from '../types/incidents';
import { FLEET_OVERVIEW_STALE_MS } from './useFleetMetrics';

export const incidentsKeys = {
  all: ['incidents'] as const,
  list: (query: IncidentsQuery) =>
    [
      'incidents',
      'list',
      query.dateFrom,
      query.dateTo,
      query.kind ?? '',
      query.outcome ?? '',
      query.printerId ?? 0,
      query.limit,
      query.offset,
    ] as const,
};

const UNRESOLVED: IncidentsQuery = { dateFrom: '', dateTo: '', limit: 0, offset: 0 };

/**
 * One page of the ledger. `query` is `undefined` until the window resolves
 * (the site's today comes from `/fleet-metrics/status`), and the query does
 * not run until then.
 */
export function useIncidents(query: IncidentsQuery | undefined): UseQueryResult<IncidentsResponse, Error> {
  return useQuery<IncidentsResponse, Error>({
    // Safe: `enabled` gates the call, and the key is only read when it runs.
    queryKey: incidentsKeys.list(query ?? UNRESOLVED),
    queryFn: () => api.getIncidents(query!),
    enabled: query !== undefined,
    staleTime: FLEET_OVERVIEW_STALE_MS,
    placeholderData: keepPreviousData,
  });
}
