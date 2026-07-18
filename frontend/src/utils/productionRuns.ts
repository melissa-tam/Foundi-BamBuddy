/**
 * Pure derivations over a production run. Lives outside the RunBadges component
 * module so it can be shared by pages and components without tripping
 * react-refresh's "only export components" rule.
 */
import type { ProductionRun } from '../types/productionRuns';
import { parseUTCDate } from './date';

/**
 * Whether a run is in its one-time deferred-start window: still `active` and its
 * derived start time is in the future (Phase 5). Gates the "Scheduled" badge and
 * the Start-now / Reschedule actions. A paused run reports its own status, not
 * "scheduled" — the operator's hold wins.
 */
export function isScheduled(run: Pick<ProductionRun, 'status' | 'scheduled_start_at'>): boolean {
  if (run.status !== 'active' || !run.scheduled_start_at) return false;
  const at = parseUTCDate(run.scheduled_start_at)?.getTime();
  return at != null && at > Date.now();
}
