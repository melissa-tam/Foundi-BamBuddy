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
import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  CalendarClock,
  Factory,
  Hand,
  Loader2,
  Pause,
  Play,
  RotateCcw,
  Square,
  Zap,
} from 'lucide-react';
import { api, type PrinterStatus } from '../api/client';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { RunRescheduleDialog } from '../components/RunRescheduleDialog';
import { FirstArticleBanner } from '../components/FirstArticleBanner';
import {
  BlockedPrintersChip,
  PauseReasonChip,
  RunStagedBanner,
  RunStatusBadge,
  ScheduledChip,
} from '../components/RunBadges';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { isScheduled } from '../utils/productionRuns';
import { deriveFarmPhase } from '../utils/farmPhase';
import { formatDateTime, formatRelativeTime, parseUTCDate } from '../utils/date';
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
  if (state.filament_short_live) {
    reasons.push(
      state.filament_short_detail
        ? `${t('productionRuns.detail.printerState.filamentShort')} — ${state.filament_short_detail}`
        : t('productionRuns.detail.printerState.filamentShort'),
    );
  }
  if (state.no_usb_drive) reasons.push(t('productionRuns.detail.printerState.noUsbDrive'));
  // Capability-gate reason is already a human-readable backend sentence.
  if (state.capability_reason) reasons.push(state.capability_reason);
  if (!state.connected) reasons.push(t('productionRuns.detail.printerState.offline'));

  // Cooldown/eject phase (Phase 4.3c) from the live status cache.
  const phase = deriveFarmPhase({
    state: status?.state,
    awaiting_plate_clear: state.awaiting_plate_clear,
    eject_watch: status?.eject_watch,
  });
  let phaseText: string | null = null;
  if (phase?.kind === 'printing') phaseText = t('printers.phase.printing');
  else if (phase?.kind === 'cooling') {
    // Never claim "cooling" for a printer we cannot observe — mirror the
    // printer card, which shows no phase pill while disconnected.
    if (state.connected) {
      phaseText = t('printers.phase.cooling', { threshold: Math.round(phase.threshold) });
    }
  } else if (phase?.kind === 'awaitingPlateClear') phaseText = t('printers.phase.awaitingPlateClear');

  const blocked =
    state.quarantined ||
    state.model_mismatch ||
    state.stalled ||
    state.vision_hold ||
    state.filament_short_live ||
    state.no_usb_drive ||
    state.capability_reason != null ||
    !state.connected;
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
// Not-eligible panel (immediate dispatch feedback)
// ---------------------------------------------------------------------------

/**
 * The reasons — as short translated labels — that make one printer ineligible
 * to take a unit from this run right now. Derived from the SAME live flags the
 * chips read (no extra API call): offline, quarantined, awaiting-plate-clear,
 * model mismatch, live filament shortage, no USB drive, and the capability
 * gate. An empty list means the printer is eligible. Busy / stagger-hold are
 * NOT listed — they self-resolve. `capability_reason` and `filament_short_detail`
 * are backend-authored human sentences and render verbatim.
 */
function eligibilityReasons(state: RunPrinterState, t: (k: string) => string): string[] {
  const reasons: string[] = [];
  if (!state.connected) reasons.push(t('productionRuns.detail.eligibility.offline'));
  if (state.quarantined) reasons.push(t('productionRuns.detail.eligibility.quarantined'));
  if (state.awaiting_plate_clear) reasons.push(t('productionRuns.detail.eligibility.awaitingPlateClear'));
  if (state.model_mismatch) {
    reasons.push(
      state.model_mismatch_reason
        ? `${t('productionRuns.detail.eligibility.modelMismatch')} — ${state.model_mismatch_reason}`
        : t('productionRuns.detail.eligibility.modelMismatch'),
    );
  }
  if (state.filament_short_live) {
    reasons.push(
      state.filament_short_detail
        ? `${t('productionRuns.detail.eligibility.filamentShort')} — ${state.filament_short_detail}`
        : t('productionRuns.detail.eligibility.filamentShort'),
    );
  }
  if (state.no_usb_drive) reasons.push(t('productionRuns.detail.eligibility.noUsbDrive'));
  if (state.capability_reason) reasons.push(state.capability_reason);
  return reasons;
}

/**
 * Banner card listing every printer the run targets that won't participate yet,
 * with each printer's blocking reasons. Renders nothing when every printer is
 * eligible (no empty-state card). Mirrors the RunStagedBanner tone/styling and
 * reuses the chips' red "blocked" palette; the detail query's 5 s poll + WS
 * invalidation keep it live, so a resolved printer drops off on the next fetch.
 */
function NotEligibleBanner({ printerStates }: { printerStates: RunPrinterState[] }) {
  const { t } = useTranslation();
  const ineligible = printerStates
    .map((state) => ({ state, reasons: eligibilityReasons(state, t) }))
    .filter((entry) => entry.reasons.length > 0);

  if (ineligible.length === 0) return null;

  return (
    <Card>
      <CardContent>
        <div className="flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/10 p-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-300" aria-hidden="true" />
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-red-200">
              {t('productionRuns.detail.eligibility.title')}
            </h2>
            <p className="mt-0.5 text-xs text-red-300/90">
              {t('productionRuns.detail.eligibility.description')}
            </p>
            <ul className="mt-2 space-y-2">
              {ineligible.map(({ state, reasons }) => (
                <li key={state.printer_id}>
                  <span className="text-sm font-medium text-white">{state.name}</span>
                  <ul className="mt-0.5 space-y-0.5">
                    {reasons.map((reason) => (
                      <li key={reason} className="text-xs text-red-300">
                        {reason}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </CardContent>
    </Card>
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
        {unit.status === 'pending' &&
          unit.scheduled_time &&
          (parseUTCDate(unit.scheduled_time)?.getTime() ?? 0) > Date.now() && (
            <p className="mt-1 flex items-center gap-1 text-xs text-indigo-300">
              <CalendarClock className="h-3 w-3" aria-hidden="true" />
              {t('productionRuns.schedule.startsIn', {
                when: formatRelativeTime(unit.scheduled_time, 'system', t),
              })}
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

  const canUpdate = hasPermission('production_runs:update');
  // Abort behind a confirm; reschedule opens the shared dialog.
  const [abortConfirmOpen, setAbortConfirmOpen] = useState(false);
  const [rescheduleOpen, setRescheduleOpen] = useState(false);

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

  // Lifecycle mutations all invalidate the ['production-runs'] tree, which by
  // TanStack's prefix match also refreshes this run's ['production-runs', id]
  // detail query (the 5 s poll + `production_run_changed` WS invalidation keep
  // it live regardless).
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['production-runs'] });

  const resumeMutation = useMutation({
    mutationFn: () => api.resumeProductionRun(runId),
    onSuccess: () => {
      showToast(t('productionRuns.resumed'));
      invalidate();
    },
    onError: (err: Error) => showToast(err.message || t('productionRuns.actionFailed'), 'error'),
  });

  const pauseMutation = useMutation({
    mutationFn: () => api.pauseProductionRun(runId),
    onSuccess: () => {
      showToast(t('productionRuns.paused'));
      invalidate();
    },
    onError: (err: Error) => showToast(err.message || t('productionRuns.actionFailed'), 'error'),
  });

  const abortMutation = useMutation({
    mutationFn: () => api.abortProductionRun(runId),
    onSuccess: () => {
      showToast(t('productionRuns.aborted'));
      invalidate();
      setAbortConfirmOpen(false);
    },
    onError: (err: Error) => {
      showToast(err.message || t('productionRuns.actionFailed'), 'error');
      setAbortConfirmOpen(false);
    },
  });

  // Reschedule / Start-now share one endpoint (Phase 5): a future ISO
  // reschedules, null starts the run now (Start-now toasts differently).
  const rescheduleMutation = useMutation({
    mutationFn: (at: string | null) => api.rescheduleProductionRun(runId, at),
    onSuccess: (_run, at) => {
      showToast(at ? t('productionRuns.rescheduled') : t('productionRuns.startedNow'));
      invalidate();
      setRescheduleOpen(false);
    },
    onError: (err: Error) => showToast(err.message || t('productionRuns.actionFailed'), 'error'),
  });

  // Any lifecycle mutation in flight disables the whole header action cluster.
  const mutating =
    resumeMutation.isPending ||
    pauseMutation.isPending ||
    abortMutation.isPending ||
    rescheduleMutation.isPending;

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
                    <RunStatusBadge status={run.status} scheduledStartAt={run.scheduled_start_at} />
                    <PauseReasonChip run={run} />
                    <BlockedPrintersChip run={run} />
                    <ScheduledChip run={run} />
                  </div>
                  {run.sku_code && <p className="mt-2 text-sm text-gray-400">{run.sku_code}</p>}
                </div>
                {/* Lifecycle controls — mirror the list card so the detail page
                    is no longer a dead end. A scheduled (not-yet-started) run
                    offers Start-now + Reschedule; a running run Pause; a paused
                    or operator-stopped run Resume; any non-terminal run Abort. */}
                <div className="flex items-center gap-1 shrink-0">
                  {isScheduled(run) && canUpdate && (
                    <>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => rescheduleMutation.mutate(null)}
                        disabled={mutating}
                        aria-label={t('productionRuns.startNow')}
                      >
                        {mutating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Zap className="h-4 w-4" />}
                        {t('productionRuns.startNow')}
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => {
                          rescheduleMutation.reset();
                          setRescheduleOpen(true);
                        }}
                        disabled={mutating}
                        aria-label={t('productionRuns.reschedule')}
                      >
                        <CalendarClock className="h-4 w-4" />
                        {t('productionRuns.reschedule')}
                      </Button>
                    </>
                  )}
                  {run.status === 'active' &&
                    !isScheduled(run) &&
                    run.pause_reason !== 'operator_stop' &&
                    canUpdate && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => pauseMutation.mutate()}
                      disabled={mutating}
                      aria-label={t('productionRuns.pause')}
                    >
                      {pauseMutation.isPending ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Pause className="h-4 w-4" />
                      )}
                      {t('productionRuns.pause')}
                    </Button>
                  )}
                  {(run.status === 'paused' || run.pause_reason === 'operator_stop') && canUpdate && (
                    <Button
                      size="sm"
                      onClick={() => resumeMutation.mutate()}
                      disabled={mutating}
                      aria-label={t('productionRuns.resume')}
                    >
                      {resumeMutation.isPending ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Play className="h-4 w-4" />
                      )}
                      {t('productionRuns.resume')}
                    </Button>
                  )}
                  {run.status !== 'completed' && run.status !== 'cancelled' && canUpdate && (
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => setAbortConfirmOpen(true)}
                      disabled={mutating}
                      aria-label={t('productionRuns.abort')}
                    >
                      <Square className="h-4 w-4" />
                      {t('productionRuns.abort')}
                    </Button>
                  )}
                </div>
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

          {/* Not-eligible feedback (immediate on send): which targeted printers
              won't participate yet, and why. Above the chips per operator
              placement; self-hides when every printer is eligible. */}
          <NotEligibleBanner printerStates={printerStates} />

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

          {abortConfirmOpen && (
            <ConfirmModal
              title={t('productionRuns.abortTitle')}
              message={t('productionRuns.abortBody', { name: run.name })}
              confirmText={t('productionRuns.abort')}
              cancelText={t('common.cancel')}
              variant="danger"
              isLoading={abortMutation.isPending}
              onConfirm={() => abortMutation.mutate()}
              onCancel={() => setAbortConfirmOpen(false)}
            />
          )}

          {rescheduleOpen && (
            <RunRescheduleDialog
              run={run}
              saving={rescheduleMutation.isPending}
              error={
                rescheduleMutation.error
                  ? rescheduleMutation.error.message || t('productionRuns.actionFailed')
                  : null
              }
              onSubmit={(at) => rescheduleMutation.mutate(at)}
              onClose={() => {
                rescheduleMutation.reset();
                setRescheduleOpen(false);
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

export default ProductionRunDetailPage;
