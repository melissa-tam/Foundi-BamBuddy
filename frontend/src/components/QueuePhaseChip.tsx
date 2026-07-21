/**
 * QueuePhaseChip (Phase C latency-reduction) — a compact live dispatch-phase
 * chip for a queue row. Fills the otherwise-dead window between "queued" and
 * "printing" (FTPS upload ~6 s + printer firmware prep ~45 s) with concrete
 * feedback: "Uploading 62 %", "Sent", "Preparing…", or an error "Failed".
 *
 * Self-contained like OutOfRotationChip: it subscribes to the WS-populated
 * `['queueItemPhase', itemId]` query cache (written by useWebSocket) so the row
 * needs no extra props. The queryFn only reads the current cache value — the
 * live updates arrive via `setQueryData`, which re-renders this observer.
 *
 * Accessibility: the progress is in the visible text (not colour-only); the
 * chip pairs an icon with the label and mirrors the label into aria-label.
 * The `printing` phase is suppressed — the row's StatusBadge + progress bar
 * already own that state, so showing a chip too would be a permanent duplicate.
 */
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Upload, Send, CheckCircle2, XCircle } from 'lucide-react';
import type { QueueItemPhaseState } from '../api/client';

export function QueuePhaseChip({ itemId }: { itemId: number }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Cache-only subscription: never fetches (staleTime Infinity, no interval);
  // the WS handler's setQueryData is the sole writer and drives re-renders.
  const { data } = useQuery<QueueItemPhaseState | null>({
    queryKey: ['queueItemPhase', itemId],
    queryFn: () =>
      queryClient.getQueryData<QueueItemPhaseState>(['queueItemPhase', itemId]) ?? null,
    staleTime: Infinity,
  });

  if (!data) return null;
  const { phase, progress_pct } = data;
  if (phase === 'printing') return null;

  let label: string;
  let Icon = Loader2;
  let spin = false;
  switch (phase) {
    case 'assigned':
      label = t('queue.phase.assigned');
      Icon = CheckCircle2;
      break;
    case 'uploading':
      label =
        progress_pct != null
          ? t('queue.phase.uploading', { pct: Math.round(progress_pct) })
          : t('queue.phase.uploadingNoPct');
      Icon = Upload;
      break;
    case 'sent':
      label = t('queue.phase.sent');
      Icon = Send;
      break;
    case 'preparing':
      label = t('queue.phase.preparing');
      Icon = Loader2;
      spin = true;
      break;
    case 'failed':
      label = t('queue.phase.failed');
      Icon = XCircle;
      break;
    default:
      return null;
  }

  const toneClass =
    phase === 'failed'
      ? 'text-red-700 dark:text-red-400 bg-red-400/10 border-red-400/20'
      : 'text-blue-700 dark:text-blue-400 bg-blue-400/10 border-blue-400/20';

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] sm:text-xs font-medium border ${toneClass}`}
      aria-label={label}
    >
      <Icon className={`w-3 h-3 ${spin ? 'animate-spin' : ''}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export default QueuePhaseChip;
