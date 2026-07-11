/**
 * Production run detail page (Phase 4.1) — answers "what is this run doing and
 * why" at a glance: header with status + hold reason, plate progress, one
 * blocked-state chip per printer (with the live cooldown phase), the staged
 * banner, and the per-unit table (stop attribution, waiting reasons, retry
 * lineage, error messages).
 *
 * Data: GET /production-runs/{id} (5 s poll + `production_run_changed` WS
 * invalidation) for the run; the printers' live phase (eject_watch + bed) reads
 * the shared ['printerStatus', id] caches the WebSocket keeps warm.
 */
import { useMemo } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  ArrowLeft,
  Factory,
  Hand,
  Loader2,
  Play,
  RotateCcw,
} from 'lucide-react';
import { api, type PrinterStatus } from '../api/client';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { FirstArticleBanner } from '../components/FirstArticleBanner';
import {
  BlockedPrintersChip,
  PauseReasonChip,
  RunStagedBanner,
  RunStatusBadge,
} from '../components/RunBadges';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { deriveFarmPhase } from '../utils/farmPhase';
import { formatDateTime } from '../utils/date';
import { waitingReasonText } from '../utils/waitingReason';
import type { ProductionRun, RunPrinterState, RunUnit } from '../types/productionRuns';

// ---------------------------------------------------------------------------
// Unit status pill
// ---------------------------------------------------------------------------

const UNIT_STATUS_STYLES: Record<RunUnit['status'], string> = {
  pending: 'bg-bambu-dark text-gray-400 border-bambu-dark-tertiary',
  printing: 'bg-bambu-green/15 text-bambu-green border-bambu-green/30',
  completed: 'bg-blue-500/15 text-blue-300 border-blue-500/30',
  failed: 'bg-red-500/15 text-red-300 border-red-500/30',
  skipped: 'bg-orange-500/15 text-orange-300 border-orange-500/30',
  cancelled: 'bg-bambu-dark text-gray-400 border-bambu-dark-tertiary',
};

function UnitStatusPill({ status }: { status: RunUnit['status'] }) {
  const { t } = useTranslation();
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${UNIT_STATUS_STYLES[status]}`}
    >
      {t(`productionRuns.detail.unitStatus.${status}`)}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Printer-state chip (one per printer the run targets)
// ---------------------------------------------------------------------------

function PrinterStateChip({ state, status }: { state: RunPrinterState; status?: PrinterStatus }) {
  const { t } = useTranslation();

  // Every live reason this printer can't take (or is between) run units.
  const reasons: string[] = [];
  if (state.quarantined) reasons.push(t('productionRuns.detail.printerState.quarantined'));
  if (state.model_mismatch) {
    reasons.push(
      state.model_mismatch_reason
        ? `${t('productionRuns.detail.printerState.modelMismatch')} — ${state.model_mismatch_reason}`
        : t('productionRuns.detail.printerState.modelMismatch'),
    );
  }
  if (state.stalled) reasons.push(t('productionRuns.detail.printerState.stalled'));
  if (state.vision_hold) reasons.push(t('productionRuns.detail.printerState.visionHold'));
  if (!state.connected) reasons.push(t('productionRuns.detail.printerState.offline'));

  // Cooldown/eject phase (Phase 4.3c) from the live status cache.
  const phase = deriveFarmPhase({
    state: status?.state,
    awaiting_plate_clear: state.awaiting_plate_clear,
    eject_watch: status?.eject_watch,
    bed: status?.temperatures?.bed,
  });
  let phaseText: string | null = null;
  if (phase?.kind === 'printing') phaseText = t('printers.phase.printing');
  else if (phase?.kind === 'cooling') {
    phaseText =
      phase.bed != null
        ? t('printers.phase.cooling', { threshold: Math.round(phase.threshold), bed: Math.round(phase.bed) })
        : t('printers.phase.coolingNoBed', { threshold: Math.round(phase.threshold) });
  } else if (phase?.kind === 'awaitingPlateClear') phaseText = t('printers.phase.awaitingPlateClear');

  const blocked = state.quarantined || state.model_mismatch || state.stalled || state.vision_hold || !state.connected;
  const tone = blocked
    ? 'border-red-500/40 bg-red-500/10'
    : state.awaiting_plate_clear
      ? 'border-yellow-500/40 bg-yellow-500/10'
      : 'border-bambu-dark-tertiary bg-bambu-dark';

  return (
    <div className={`rounded-lg border p-2.5 ${tone}`}>
      <div className="flex items-center gap-2">
        <span
          className={`h-2 w-2 flex-shrink-0 rounded-full ${
            blocked ? 'bg-red-400' : state.connected ? 'bg-bambu-green' : 'bg-gray-400'
          }`}
          aria-hidden="true"
        />
        <span className="truncate text-sm font-medium text-white">{state.name}</span>
      </div>
      {phaseText && <p className="mt-1 text-xs text-gray-400">{phaseText}</p>}
      {reasons.length > 0 ? (
        <ul className="mt-1 space-y-0.5">
          {reasons.map((r) => (
            <li key={r} className="text-xs text-red-300">
              {r}
            </li>
          ))}
        </ul>
      ) : (
        !phaseText && <p className="mt-1 text-xs text-gray-400">{t('productionRuns.detail.printerState.ok')}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Unit table row
// ---------------------------------------------------------------------------

function UnitRow({ unit }: { unit: RunUnit }) {
  const { t } = useTranslation();
  const waiting = waitingReasonText(unit.waiting_reason, t);
  return (
    <tr className="border-b border-bambu-dark-tertiary/60 last:border-0">
      <td className="px-3 py-2 text-sm tabular-nums text-gray-400">#{unit.id}</td>
      <td className="px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <UnitStatusPill status={unit.status} />
          {unit.stop_source && (
            <span className="inline-flex items-center gap-1 rounded-full border border-orange-500/30 bg-orange-500/15 px-2 py-0.5 text-xs font-medium text-orange-300">
              <Hand className="h-3 w-3" aria-hidden="true" />
              {t('productionRuns.detail.stoppedByOperator')}
            </span>
          )}
          {unit.first_article && (
            <span className="inline-flex items-center rounded-full border border-cyan-500/30 bg-cyan-500/15 px-2 py-0.5 text-xs font-medium text-cyan-300">
              {t('productionRuns.detail.firstArticleBadge')}
            </span>
          )}
          {unit.status === 'pending' && unit.manual_start && (
            <span className="inline-flex items-center rounded-full border border-purple-500/30 bg-purple-500/15 px-2 py-0.5 text-xs font-medium text-purple-300">
              {unit.filament_short
                ? t('productionRuns.detail.lowSpoolBadge')
                : t('productionRuns.detail.stagedBadge')}
            </span>
          )}
        </div>
        {unit.retry_of_id != null && (
          <p className="mt-1 flex items-center gap-1 text-xs text-gray-400">
            <RotateCcw className="h-3 w-3" aria-hidden="true" />
            {t('productionRuns.detail.retryOf', { count: unit.retry_count, id: unit.retry_of_id })}
          </p>
        )}
        {waiting && <p className="mt-1 text-xs text-purple-300">{waiting}</p>}
        {unit.error_message && (
          <p className="mt-1 max-w-md truncate text-xs text-red-400" title={unit.error_message}>
            {unit.error_message}
          </p>
        )}
      </td>
      <td className="px-3 py-2 text-sm text-gray-400">
        {unit.printer_name ?? (unit.printer_id != null ? `#${unit.printer_id}` : '—')}
      </td>
      <td className="px-3 py-2 text-sm tabular-nums text-gray-400">
        {unit.completed_at
          ? formatDateTime(unit.completed_at)
          : unit.started_at
            ? formatDateTime(unit.started_at)
            : '—'}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function ProductionRunDetailPage() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const runId = Number(id);
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();

  const {
    data: run,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  } = useQuery<ProductionRun, Error>({
    queryKey: ['production-runs', runId],
    queryFn: () => api.getProductionRun(runId),
    enabled: Number.isFinite(runId),
    refetchInterval: 5000,
  });

  // Live phase inputs for the printer chips (shared cache the WebSocket keeps
  // fresh; 30 s REST fallback mirrors the printer card).
  const printerStates = useMemo(() => run?.printer_states ?? [], [run]);
  const statusQueries = useQueries({
    queries: printerStates.map((p) => ({
      queryKey: ['printerStatus', p.printer_id],
      queryFn: () => api.getPrinterStatus(p.printer_id),
      refetchInterval: 30000,
    })),
  });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeProductionRun(runId),
    onSuccess: () => {
      showToast(t('productionRuns.resumed'));
      queryClient.invalidateQueries({ queryKey: ['production-runs'] });
    },
    onError: (err: Error) => showToast(err.message || t('productionRuns.actionFailed'), 'error'),
  });

  const notFound = isError && /not found/i.test(error?.message ?? '');
  const units = run?.units ?? [];

  return (
    <div className="mx-auto max-w-5xl p-6">
      <Link
        to="/production-runs"
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-gray-400 transition-colors hover:text-white"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        {t('productionRuns.detail.back')}
      </Link>

      {isLoading ? (
        <div className="flex items-center justify-center py-16 text-gray-400" role="status">
          <Loader2 className="h-6 w-6 animate-spin" />
          <span className="ml-2">{t('common.loading')}</span>
        </div>
      ) : isError || !run ? (
        <Card>
          <CardContent className="flex flex-col items-center py-12 text-center">
            <AlertCircle className="mb-3 h-10 w-10 text-red-400" />
            <p className="mb-4 text-white">
              {notFound ? t('productionRuns.detail.notFound') : t('productionRuns.detail.loadError')}
            </p>
            {!notFound && (
              <Button variant="secondary" onClick={() => refetch()} disabled={isFetching}>
                {isFetching ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                {t('common.retry')}
              </Button>
            )}
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {/* Header */}
          <Card>
            <CardContent>
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h1 className="flex items-center gap-2 text-xl font-bold text-white">
                    <Factory className="h-5 w-5 flex-shrink-0 text-bambu-green" aria-hidden="true" />
                    <span className="truncate">{run.name}</span>
                  </h1>
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <RunStatusBadge status={run.status} />
                    <PauseReasonChip run={run} />
                    <BlockedPrintersChip run={run} />
                  </div>
                  {run.sku_code && <p className="mt-2 text-sm text-gray-400">{run.sku_code}</p>}
                </div>
                {(run.status === 'paused' || run.pause_reason === 'operator_stop') &&
                  hasPermission('production_runs:update') && (
                    <Button
                      size="sm"
                      onClick={() => resumeMutation.mutate()}
                      disabled={resumeMutation.isPending}
                    >
                      {resumeMutation.isPending ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Play className="h-4 w-4" />
                      )}
                      {t('productionRuns.resume')}
                    </Button>
                  )}
              </div>

              {/* Progress */}
              <div className="mt-4">
                <div className="mb-1 flex items-center justify-between text-xs text-gray-400">
                  <span>
                    {t('productionRuns.platesProgress', {
                      completed: run.plates_completed,
                      total: run.plates_total,
                    })}
                  </span>
                  <span>
                    {t('productionRuns.stats.units')}: {run.units_completed} / {run.units_planned}
                  </span>
                </div>
                <div
                  className="h-2 overflow-hidden rounded-full bg-bambu-dark"
                  role="progressbar"
                  aria-valuenow={run.plates_total > 0 ? Math.round((run.plates_completed / run.plates_total) * 100) : 0}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={t('productionRuns.platesProgress', {
                    completed: run.plates_completed,
                    total: run.plates_total,
                  })}
                >
                  <div
                    className="h-full bg-bambu-green transition-all"
                    style={{
                      width: `${run.plates_total > 0 ? Math.min(100, Math.round((run.plates_completed / run.plates_total) * 100)) : 0}%`,
                    }}
                  />
                </div>
              </div>

              <RunStagedBanner run={run} />

              {/* First-article approval gate (Phase 4, F1): self-contained here
                  with the part photo + collapsible camera so a remote approver
                  can act without leaving the run detail. */}
              <FirstArticleBanner run={run} />
            </CardContent>
          </Card>

          {/* Printer states */}
          <Card>
            <CardContent>
              <h2 className="mb-3 text-sm font-semibold text-white">
                {t('productionRuns.detail.printersTitle')}
              </h2>
              {printerStates.length === 0 ? (
                <p className="text-sm italic text-gray-400">{t('productionRuns.detail.noPrinters')}</p>
              ) : (
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {printerStates.map((p, i) => (
                    <PrinterStateChip key={p.printer_id} state={p} status={statusQueries[i]?.data} />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Units */}
          <Card>
            <CardContent>
              <h2 className="mb-3 text-sm font-semibold text-white">{t('productionRuns.detail.unitsTitle')}</h2>
              {units.length === 0 ? (
                <p className="text-sm italic text-gray-400">{t('productionRuns.detail.noUnits')}</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-left">
                    <thead>
                      <tr className="border-b border-bambu-dark-tertiary text-xs uppercase tracking-wide text-gray-400">
                        <th scope="col" className="px-3 py-2 font-medium">
                          {t('productionRuns.detail.unitCol')}
                        </th>
                        <th scope="col" className="px-3 py-2 font-medium">
                          {t('productionRuns.detail.statusCol')}
                        </th>
                        <th scope="col" className="px-3 py-2 font-medium">
                          {t('productionRuns.detail.printerCol')}
                        </th>
                        <th scope="col" className="px-3 py-2 font-medium">
                          {t('productionRuns.detail.timeCol')}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {units.map((u) => (
                        <UnitRow key={u.id} unit={u} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}

export default ProductionRunDetailPage;
