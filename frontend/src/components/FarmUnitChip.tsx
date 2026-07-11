/**
 * FarmUnitChip (Phase 3, F2) — on a printer card, explains why the printer is
 * doing (or blocked on) farm work without opening the run detail.
 *
 * Renders the owning run as a link to its detail page plus ONE status line: the
 * printing unit, a staged hold (low-spool variant when filament is short), a
 * waiting-reason machine code (via the shared `waitingReasonText`), or the last
 * unit's failure. Returns null when the printer holds no farm context — the
 * chip is supplementary and simply absent otherwise.
 */
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Factory } from 'lucide-react';
import type { FarmPrinterContext } from '../api/client';
import { waitingReasonText } from '../utils/waitingReason';

interface FarmStatusLine {
  text: string;
  tone: 'normal' | 'muted' | 'danger';
  /** Full text for the `title` tooltip when the visible text is truncated. */
  title?: string;
}

function deriveStatusLine(
  ctx: FarmPrinterContext,
  t: (key: string, opts?: Record<string, unknown>) => string,
): FarmStatusLine | null {
  const waiting = waitingReasonText(ctx.waiting_reason, t);

  if (ctx.unit_status === 'printing') {
    // A stall/vision hold on the live print is more informative than "printing".
    if (waiting) return { text: waiting, tone: 'danger' };
    return {
      text: t('printers.farm.printingUnit', { id: ctx.unit_id }),
      tone: 'normal',
    };
  }

  if (ctx.unit_status === 'pending') {
    if (ctx.staged) {
      return ctx.filament_short
        ? { text: t('printers.farm.stagedLowSpool'), tone: 'danger' }
        : { text: t('printers.farm.staged'), tone: 'muted' };
    }
    if (waiting) return { text: waiting, tone: 'danger' };
    return null;
  }

  if (ctx.unit_status === 'failed' && ctx.error_message) {
    return {
      text: t('printers.farm.lastUnitFailed', { error: ctx.error_message }),
      tone: 'danger',
      title: ctx.error_message,
    };
  }

  return null;
}

const TONE_CLASS: Record<FarmStatusLine['tone'], string> = {
  normal: 'text-gray-300',
  muted: 'text-gray-400',
  danger: 'text-red-400',
};

export function FarmUnitChip({ ctx }: { ctx?: FarmPrinterContext | null }) {
  const { t } = useTranslation();
  if (!ctx) return null;

  const status = deriveStatusLine(ctx, t);

  return (
    <div className="mb-3 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-2.5">
      <div className="flex items-center gap-2">
        <Factory className="h-4 w-4 flex-shrink-0 text-bambu-green" aria-hidden="true" />
        <Link
          to={`/production-runs/${ctx.run_id}`}
          onClick={(e) => e.stopPropagation()}
          className="truncate text-xs font-medium text-bambu-green transition-colors hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green focus-visible:ring-offset-2 focus-visible:ring-offset-bambu-dark"
          title={ctx.run_name}
        >
          {t('printers.farm.run', { name: ctx.run_name })}
        </Link>
      </div>
      {status && (
        <p className={`mt-1 truncate text-xs ${TONE_CLASS[status.tone]}`} title={status.title}>
          {status.text}
        </p>
      )}
    </div>
  );
}

export default FarmUnitChip;
