/**
 * EjectPhaseChip (Phase C latency-reduction) — a compact live eject-phase chip
 * for a printer card. During a manual/auto eject the plate looks idle for tens
 * of seconds (3MF repack + FTPS upload + sweep); this surfaces the live phase:
 * "Ejecting: uploading 62 %", "Ejecting: sweeping…", or "Eject failed".
 *
 * Self-contained like OutOfRotationChip: it subscribes to the WS-populated
 * `['ejectProgress', printerId]` query cache (written + TTL-cleared by
 * useWebSocket). The queryFn only reads the current cache value; live updates
 * arrive via `setQueryData`. Renders nothing when no eject is in flight.
 *
 * Accessibility: progress is in the visible text (not colour-only); icon +
 * label, label mirrored into aria-label.
 */
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, XCircle } from 'lucide-react';
import type { EjectPhaseState } from '../api/client';

export function EjectPhaseChip({ printerId }: { printerId: number }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const { data } = useQuery<EjectPhaseState | null>({
    queryKey: ['ejectProgress', printerId],
    queryFn: () =>
      queryClient.getQueryData<EjectPhaseState>(['ejectProgress', printerId]) ?? null,
    staleTime: Infinity,
  });

  if (!data) return null;
  const { phase, progress_pct } = data;

  let label: string;
  switch (phase) {
    case 'building':
      label = t('printers.ejectPhase.building');
      break;
    case 'uploading':
      label =
        progress_pct != null
          ? t('printers.ejectPhase.uploading', { pct: Math.round(progress_pct) })
          : t('printers.ejectPhase.uploadingNoPct');
      break;
    case 'sent':
      label = t('printers.ejectPhase.sent');
      break;
    case 'sweeping':
      label = t('printers.ejectPhase.sweeping');
      break;
    case 'failed':
      label = t('printers.ejectPhase.failed');
      break;
    default:
      return null;
  }

  const isFailed = phase === 'failed';
  const toneClass = isFailed
    ? 'bg-red-400/15 text-red-300 border-red-400/30'
    : 'bg-blue-400/15 text-blue-300 border-blue-400/30';
  const Icon = isFailed ? XCircle : Loader2;

  return (
    <span
      className={`inline-flex flex-shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium ${toneClass}`}
      aria-label={label}
    >
      <Icon className={`h-3 w-3 ${isFailed ? '' : 'animate-spin'}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export default EjectPhaseChip;
