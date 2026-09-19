/**
 * THE `['production-runs', id]` query — one run's DETAIL response
 * (`GET /production-runs/{id}`, the only response carrying `printer_states`
 * and `units`). Every surface that needs a run's detail goes through this hook;
 * no page declares its own `useQuery` on that key (pinned by
 * `__tests__/hooks/useProductionRunDetail.test.ts`).
 *
 * Two callers with deliberately different appetites:
 *
 * - The run **detail page** polls (`poll: true`). It is the whole screen.
 * - A **run card** on the list does NOT poll (`poll: false`) and is refreshed by
 *   the `production_run_changed` websocket, which invalidates `['production-runs']`
 *   and — TanStack's default prefix match — this key with it. The backend's
 *   `_build_printer_eligibility` runs a per-printer deficit computation, so
 *   polling it once per open card every 5 s is work the websocket already does
 *   on the only occasions that change the answer.
 *
 * `staleTime` matches the poll interval so a run whose body is already in the
 * cache — `POST /production-runs` returns the same body as the GET, and the
 * runs page seeds it — is not re-fetched on mount. An invalidation still
 * refetches immediately: `invalidateQueries` ignores `staleTime`.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';
import { api } from '../api/client';
import type { ProductionRun } from '../types/productionRuns';

/** The detail page's poll period, and the freshness window for every caller. */
export const PRODUCTION_RUN_DETAIL_POLL_MS = 5000;

export interface UseProductionRunDetailOptions {
  /** Skip the fetch entirely (a collapsed panel, or an unparsable route id). */
  enabled?: boolean;
  /** Poll every `PRODUCTION_RUN_DETAIL_POLL_MS`; false = websocket-driven only. */
  poll?: boolean;
}

export function useProductionRunDetail(
  runId: number,
  { enabled = true, poll = true }: UseProductionRunDetailOptions = {},
): UseQueryResult<ProductionRun, Error> {
  return useQuery<ProductionRun, Error>({
    queryKey: ['production-runs', runId],
    queryFn: () => api.getProductionRun(runId),
    enabled: enabled && Number.isFinite(runId),
    refetchInterval: poll ? PRODUCTION_RUN_DETAIL_POLL_MS : false,
    staleTime: PRODUCTION_RUN_DETAIL_POLL_MS,
  });
}
