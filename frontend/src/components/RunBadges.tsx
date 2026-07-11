/**
 * Shared hold-visibility pieces for production runs (Phase 4.1): the
 * pause-reason chip, the blocked-printers chip and the staged banner. One
 * implementation used by both the runs list card and the run detail page so
 * the two surfaces can never drift.
 */
import { useTranslation } from 'react-i18next';
import { AlertTriangle, Hand, PauseCircle, ShieldAlert } from 'lucide-react';
import type { ProductionRun, ProductionRunStatus, RunPauseReason } from '../types/productionRuns';

const STATUS_STYLES: Record<ProductionRunStatus, string> = {
  active: 'bg-bambu-green/15 text-bambu-green border-bambu-green/30',
  paused: 'bg-yellow-500/15 text-yellow-300 border-yellow-500/30',
  completed: 'bg-blue-500/15 text-blue-300 border-blue-500/30',
  cancelled: 'bg-red-500/15 text-red-300 border-red-500/30',
};

/** Run lifecycle badge (shared by the runs list card and the detail page). */
export function RunStatusBadge({ status }: { status: ProductionRunStatus }) {
  const { t } = useTranslation();
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${STATUS_STYLES[status]}`}
    >
      {t(`productionRuns.status.${status}`)}
    </span>
  );
}

const PAUSE_REASON_KEYS: Record<RunPauseReason, string> = {
  operator: 'productionRuns.pauseReason.operator',
  operator_stop: 'productionRuns.pauseReason.operator_stop',
  first_article_rejected: 'productionRuns.pauseReason.first_article_rejected',
  no_available_printers: 'productionRuns.pauseReason.no_available_printers',
};

/** Why-is-this-held chip. Rendered only while the run is active/paused —
 *  a terminal run's residual reason is history, not a live hold. */
export function PauseReasonChip({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  if (!run.pause_reason || (run.status !== 'active' && run.status !== 'paused')) return null;
  const key = PAUSE_REASON_KEYS[run.pause_reason] ?? null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-yellow-500/30 bg-yellow-500/15 px-2 py-0.5 text-xs font-medium text-yellow-300">
      <PauseCircle className="h-3.5 w-3.5" aria-hidden="true" />
      {key ? t(key) : run.pause_reason}
    </span>
  );
}

/** Presence chip for blocked printers; the full per-printer reasons live on
 *  the detail page. */
export function BlockedPrintersChip({ run }: { run: ProductionRun }) {
  const { t } = useTranslation();
  if (!run.has_blocked_printers || run.status === 'completed' || run.status === 'cancelled') return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-red-500/30 bg-red-500/15 px-2 py-0.5 text-xs font-medium text-red-300">
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
        <div className="flex items-start gap-2 rounded-lg border border-yellow-500/40 bg-yellow-500/10 p-2.5 text-sm text-yellow-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-yellow-300" aria-hidden="true" />
          <span>
            {t('productionRuns.stagedBanner.lowSpool', { count: run.staged_filament_short })}
          </span>
        </div>
      )}
      {run.staged_other > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-purple-500/40 bg-purple-500/10 p-2.5 text-sm text-purple-200">
          <Hand className="mt-0.5 h-4 w-4 flex-shrink-0 text-purple-300" aria-hidden="true" />
          <span>{t('productionRuns.stagedBanner.other', { count: run.staged_other })}</span>
        </div>
      )}
    </div>
  );
}
