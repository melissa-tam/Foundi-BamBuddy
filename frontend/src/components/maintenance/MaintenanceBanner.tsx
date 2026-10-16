/**
 * The maintenance-mode banner (the service hold). A hold is a STATE the card
 * reports beside the others, not a replacement for the status tree: the
 * session is up, so state, temps, fans, AMS and the plate pill still render
 * beneath it. Amber because the hold is the operator's own doing — red is
 * reserved for a farm-raised quarantine.
 *
 * Shows the scheduled return to service ("In maintenance until <local time>")
 * with Change and Clear, or "Set return time" when there is none; Exit ends
 * the hold now. Presentational: the host owns the exit and reschedule
 * mutations. Shared by the printer card and the touch page.
 */
import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CalendarClock, Loader2, Wrench, X } from 'lucide-react';
import type { ServiceHoldState } from '../../api/client';
import { Button } from '../Button';
import { CardContent } from '../Card';
import { Modal } from '../ui/Modal';
import { ReturnToServiceField } from './ReturnToServiceField';
import { formatDateTime, formatTimeOnly, parseUTCDate, type DateFormat, type TimeFormat } from '../../utils/date';

export interface MaintenanceBannerProps {
  printerId: number;
  printerName: string;
  hold: ServiceHoldState;
  /** Badge only — the card's compact view. */
  compact?: boolean;
  /** The viewer holds `printers:control`; every action is disabled without it. */
  canControl: boolean;
  exitPending: boolean;
  onExit: () => void;
  reschedulePending: boolean;
  /** Set (UTC ISO) or clear (`null`) the return time. Resolves on success. */
  onReschedule: (releaseAt: string | null) => Promise<unknown>;
  dateFormat?: DateFormat;
  timeFormat?: TimeFormat;
  /** Touch sizing (the touch view): 56 px actions and a touch-sized return-time dialog. */
  touch?: boolean;
}

const ACTION_CLASS =
  'inline-flex items-center gap-1 rounded-md border border-amber-400/40 bg-amber-500/20 px-2 py-1 text-xs font-medium text-amber-200 hover:bg-amber-500/30 transition-colors disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400 focus-visible:ring-offset-2 focus-visible:ring-offset-bambu-dark';

export function MaintenanceBanner({
  printerId,
  printerName,
  hold,
  compact = false,
  canControl,
  exitPending,
  onExit,
  reschedulePending,
  onReschedule,
  dateFormat = 'system',
  timeFormat = 'system',
  touch = false,
}: MaintenanceBannerProps) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const actionClass = touch ? `${ACTION_CLASS} min-h-[56px] px-4 text-base` : ACTION_CLASS;
  const since = parseUTCDate(hold.since);
  const releaseAt = hold.release_at ?? null;
  const noControl = canControl ? undefined : t('printers.permission.noControl');

  const clear = () => {
    onReschedule(null).catch(() => undefined);
  };

  return (
    <div role="status" className="mb-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-2.5">
      <div className="flex items-start gap-2">
        <Wrench className="w-4 h-4 flex-shrink-0 mt-0.5 text-amber-400" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300">
              {t('printers.maintenance.badge')}
            </span>
          </div>
          {!compact && (
            <p className="mt-1 text-xs text-amber-200/90">
              {t('printers.maintenance.since', {
                time: since ? formatTimeOnly(since, timeFormat) : t('time.unknown'),
              })}
            </p>
          )}
          {!compact && releaseAt && (
            <p className="mt-1 text-xs font-medium text-amber-100">
              {t('printers.maintenance.until', { time: formatDateTime(releaseAt, timeFormat) })}
            </p>
          )}
        </div>
      </div>
      {!compact && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); onExit(); }}
            disabled={!canControl || exitPending}
            title={noControl ?? t('printers.maintenance.exitHint')}
            className={actionClass}
          >
            {exitPending ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : null}
            {t('printers.maintenance.exitButton')}
          </button>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setEditing(true); }}
            disabled={!canControl || reschedulePending}
            title={noControl}
            aria-label={releaseAt ? t('printers.maintenance.changeReturnLabel') : undefined}
            className={actionClass}
          >
            <CalendarClock className="w-3.5 h-3.5" aria-hidden="true" />
            {releaseAt ? t('printers.maintenance.changeReturn') : t('printers.maintenance.setReturn')}
          </button>
          {releaseAt && (
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); clear(); }}
              disabled={!canControl || reschedulePending}
              title={noControl}
              aria-label={t('printers.maintenance.clearReturnLabel')}
              className={actionClass}
            >
              {t('printers.maintenance.clearReturn')}
            </button>
          )}
        </div>
      )}
      {editing && (
        <ReturnTimeDialog
          printerId={printerId}
          printerName={printerName}
          initial={releaseAt}
          saving={reschedulePending}
          onSubmit={(at) => {
            onReschedule(at)
              .then(() => setEditing(false))
              .catch(() => undefined);
          }}
          onClose={() => setEditing(false)}
          dateFormat={dateFormat}
          timeFormat={timeFormat}
          touch={touch}
        />
      )}
    </div>
  );
}

interface ReturnTimeDialogProps {
  printerId: number;
  printerName: string;
  initial: string | null;
  saving: boolean;
  onSubmit: (releaseAt: string | null) => void;
  onClose: () => void;
  dateFormat: DateFormat;
  timeFormat: TimeFormat;
  touch: boolean;
}

/** Change the return time on a held printer — the same field as the enter dialog. */
function ReturnTimeDialog({
  printerId,
  printerName,
  initial,
  saving,
  onSubmit,
  onClose,
  dateFormat,
  timeFormat,
  touch,
}: ReturnTimeDialogProps) {
  const { t } = useTranslation();
  // Button's own sizes reset the touch floor at `md:`, so the floor is restated there.
  const buttonSize = touch ? 'min-h-[64px] md:min-h-[64px] px-6 text-base' : '';
  const titleId = useId();
  const [value, setValue] = useState<string | null>(initial);
  const [valid, setValid] = useState(true);

  return (
    <Modal onClose={onClose} size="sm" widthClass={touch ? 'max-w-xl' : undefined} dismissDisabled={saving} labelledBy={titleId}>
      <CardContent className="p-0">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <CalendarClock className="w-5 h-5 text-amber-400" aria-hidden="true" />
            <h2 id={titleId} className="text-lg font-semibold text-white">
              {t('printers.maintenance.returnTitle', { name: printerName })}
            </h2>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={saving} aria-label={t('common.close')}>
            <X className="w-5 h-5" aria-hidden="true" />
          </Button>
        </div>
        <div className="p-4 space-y-4">
          <ReturnToServiceField
            value={value}
            onChange={setValue}
            onValidityChange={setValid}
            dateFormat={dateFormat}
            timeFormat={timeFormat}
            idPrefix={`maintenance-return-${printerId}`}
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose} disabled={saving} className={buttonSize}>
              {t('common.cancel')}
            </Button>
            <Button onClick={() => onSubmit(value)} disabled={saving || !valid} className={buttonSize}>
              {saving ? <Loader2 className="w-4 h-4 animate-spin" aria-hidden="true" /> : null}
              {t('common.save')}
            </Button>
          </div>
        </div>
      </CardContent>
    </Modal>
  );
}
