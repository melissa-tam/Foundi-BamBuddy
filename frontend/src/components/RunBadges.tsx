/**
 * Shared hold-visibility pieces for production runs (Phase 4.1): the
 * pause-reason chip, the blocked-printers chip and the staged banner. One
 * implementation used by both the runs list card and the run detail page so
 * the two surfaces can never drift.
 */
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CalendarClock, Hand, PauseCircle, ShieldAlert } from 'lucide-react';
import type { ProductionRun, ProductionRunStatus, RunPauseReason } from '../types/productionRuns';
import { formatRelativeTime, type TimeFormat } from '../utils/date';
import { isScheduled } from '../utils/productionRuns';
import { humanizeToken, isTokenShaped } from '../utils/waitingReason';

const STATUS_STYLES: Record<ProductionRunStatus, string> = {
  active: 'bg-bambu-green/15 text-bambu-green border-bambu-green/30',
  paused: 'bg-yellow-500/15 text-yellow-700 dark:text-yellow-300 border-yellow-500/30',
  completed: 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border-blue-500/30',
  cancelled: 'bg-red-500/15 text-red-700 dark:text-red-300 border-red-500/30',
};

const SCHEDULED_STYLE = 'bg-indigo-500/15 text-indigo-700 dark:text-indigo-300 border-indigo-500/30';

/** Run lifecycle badge (shared by the runs list card and the detail page).
 *  Renders "Scheduled" styling while the run is in its deferred-start window. */
export function RunStatusBadge({
  status,
  scheduledStartAt = null,
}: {
  status: ProductionRunStatus;
  scheduledStartAt?: string | null;
}) {
  const { t } = useTranslation();
  const scheduled = isScheduled({ status, scheduled_start_at: scheduledStartAt });
  const style = scheduled ? SCHEDULED_STYLE : STATUS_STYLES[status];
  const label = scheduled ? t('productionRuns.status.scheduled') : t(`productionRuns.status.${status}`);
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${style}`}
    >
      {label}
    </span>
  );
}

/** "Starts in 2h" chip for a scheduled run; null once it has started. */
export function ScheduledChip({ run, timeFormat = 'system' }: { run: ProductionRun; timeFormat?: TimeFormat }) {
  const { t } = useTranslation();
  if (!isScheduled(run)) return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-indigo-500/30 bg-indigo-500/15 px-2 py-0.5 text-xs font-medium text-indigo-700 dark:text-indigo-300">
      <CalendarClock className="h-3.5 w-3.5" aria-hidden="true" />
      {t('productionRuns.schedule.startsIn', { when: formatRelativeTime(run.scheduled_start_at, timeFormat, t) })}
    </span>
  );
}

const PAUSE_REASON_KEYS: Record<RunPauseReason, string> = {
  operator: 'productionRuns.pauseReason.operator',
  operator_stop: 'productionRuns.pauseReason.operator_stop',
  first_article_rejected: 'productionRuns.pauseReason.first_article_rejected',
  no_available_printers: 'productionRuns.pauseReason.no_available_printers',
  retries_exhausted: 'productionRuns.pauseReason.retries_exhausted',
};

/** Why-is-this-held chip. Rendered only while the run is active/paused —
 *  a terminal run's residual reason is history, not a live hold. */
export function PauseReasonChip({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  if (!run.pause_reason || (run.status !== 'active' && run.status !== 'paused')) return null;
  const key = PAUSE_REASON_KEYS[run.pause_reason] ?? null;
  // Fall back for an off-contract pause reason the map doesn't cover: humanize a
  // bare token (backend added a reason ahead of the frontend), else a generic
  // "see run detail" line. The raw token stays inspectable in `title`.
  const label = key
    ? t(key)
    : isTokenShaped(run.pause_reason)
      ? humanizeToken(run.pause_reason)
      : t('productionRuns.pauseReason.unknown');
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-yellow-500/30 bg-yellow-500/15 px-2 py-0.5 text-xs font-medium text-yellow-700 dark:text-yellow-300"
      title={key ? undefined : run.pause_reason}
    >
      <PauseCircle className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </span>
  );
}

/** Presence chip for blocked printers; the full per-printer reasons live on
 *  the detail page. */
export function BlockedPrintersChip({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  if (!run.has_blocked_printers || run.status === 'completed' || run.status === 'cancelled') return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-red-500/30 bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-700 dark:text-red-300">
      <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
      {t('productionRuns.blockedPrinters')}
    </span>
  );
}

/**
 * Staged-units banner. Low-spool staging (system) gets the actionable copy
 * ("swap spool, then Resume"); any other staging gets the generic hold line.
 */
export function RunStagedBanner({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  if (run.status === 'completed' || run.status === 'cancelled') return null;
  if (run.staged_filament_short <= 0 && run.staged_other <= 0) return null;
  return (
    <div className="mt-3 space-y-2">
      {run.staged_filament_short > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-yellow-500/40 bg-yellow-500/10 p-2.5 text-sm text-yellow-700 dark:text-yellow-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-yellow-700 dark:text-yellow-300" aria-hidden="true" />
          <span>
            {t('productionRuns.stagedBanner.lowSpool', { count: run.staged_filament_short })}
          </span>
        </div>
      )}
      {run.staged_other > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-purple-500/40 bg-purple-500/10 p-2.5 text-sm text-purple-700 dark:text-purple-200">
          <Hand className="mt-0.5 h-4 w-4 flex-shrink-0 text-purple-700 dark:text-purple-300" aria-hidden="true" />
          <span>{t('productionRuns.stagedBanner.other', { count: run.staged_other })}</span>
        </div>
      )}
    </div>
  );
}
