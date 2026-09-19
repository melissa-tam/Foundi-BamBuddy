/**
 * Pure derivations over a production run. Lives outside the RunBadges component
 * module so it can be shared by pages and components without tripping
 * react-refresh's "only export components" rule.
 */
import type { ProductionRun, RunPrinterState } from '../types/productionRuns';
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

/**
 * ONE reason a printer won't take a unit from this run right now. A closed set:
 * the FACT lives here, its copy lives in `components/RunEligibility`, because a
 * predicate must be able to ask "is this printer ineligible" without a `t`.
 *
 * `modelMismatch` and `filamentShort` carry an optional backend-authored detail
 * sentence; `capability` IS one. Busy / stagger-hold are deliberately absent —
 * they self-resolve.
 */
export type EligibilityReason =
  | { kind: 'offline' }
  | { kind: 'quarantined' }
  | { kind: 'awaitingPlateClear' }
  | { kind: 'modelMismatch'; detail: string | null }
  | { kind: 'filamentShort'; detail: string | null }
  | { kind: 'noUsbDrive' }
  | { kind: 'capability'; detail: string };

/**
 * Every reason one printer is ineligible, in display order. An empty list means
 * the printer is eligible. Derived from the SAME live flags the chips read — no
 * extra API call, but note these flags live on the run DETAIL response only.
 */
export function eligibilityReasons(state: RunPrinterState): EligibilityReason[] {
  const reasons: EligibilityReason[] = [];
  if (!state.connected) reasons.push({ kind: 'offline' });
  if (state.quarantined) reasons.push({ kind: 'quarantined' });
  if (state.awaiting_plate_clear) reasons.push({ kind: 'awaitingPlateClear' });
  if (state.model_mismatch) {
    reasons.push({ kind: 'modelMismatch', detail: state.model_mismatch_reason });
  }
  if (state.filament_short_live) {
    reasons.push({ kind: 'filamentShort', detail: state.filament_short_detail });
  }
  if (state.no_usb_drive) reasons.push({ kind: 'noUsbDrive' });
  if (state.capability_reason) reasons.push({ kind: 'capability', detail: state.capability_reason });
  return reasons;
}

/**
 * THE predicate behind `BlockedPrintersChip` and behind any panel paired with
 * it, so the toggle and the thing it toggles can never disagree.
 *
 * TWO sources, because the backend's list flag is NARROWER than the panel:
 * `has_blocked_printers` is computed without the filament-short, no-USB and
 * capability dimensions (they exist only on the detail response), so a run whose
 * only ineligible printers are blocked that way reports `false` on the list
 * while the panel would list them. Pass `printerStates` wherever the detail is
 * in hand and the wider truth wins; omit it and the list flag is all there is.
 *
 * A terminal run's residual flag is history, not a hold, and answers false
 * either way.
 */
export function hasLiveBlockedPrinters(
  run: Pick<ProductionRun, 'has_blocked_printers' | 'status'>,
  printerStates?: RunPrinterState[] | null,
): boolean {
  if (run.status === 'completed' || run.status === 'cancelled') return false;
  if (run.has_blocked_printers) return true;
  return (printerStates ?? []).some((state) => eligibilityReasons(state).length > 0);
}
