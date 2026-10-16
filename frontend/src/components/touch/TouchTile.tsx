/**
 * One printer on the touch view. Reads, top to bottom: a status edge in the hold's tone
 * (`HoldChip.TONE_CLASS`), the name and state words, the hold chip as the backend sent it
 * (`status.hold_chip`, never re-derived) and the maintenance hold, then either the
 * printer's camera still (a printer in error, with a plate that is not empty, cooling, or
 * in maintenance — `utils/touchOrder.stillWanted`) or its print (progress, finish time,
 * layer), the plate pill, and up to two contextual buttons plus More
 * (`utils/touchActions`). Every verb is `usePrinterActions`'; every prompt is the shared
 * `PrinterActionPrompts` at touch size.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Camera, CameraOff, MoreHorizontal, Play, RotateCw, SkipForward, Square, Wrench, CheckCircle2 } from 'lucide-react';
import type { Printer, PrinterStatus } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useCameraStill } from '../../hooks/useCameraStill';
import { usePrinterActions } from '../../hooks/usePrinterActions';
import { formatDateTime, formatETA, formatTimeOnly, parseUTCDate, type DateFormat, type TimeFormat } from '../../utils/date';
import { plateStatusOf } from '../../utils/plateStatus';
import { formatPrintName } from '../../utils/printName';
import { classifyPrinterStatus, STATUS_GROUP_META } from '../../utils/printerStatusBucket';
import type { PresetTriple } from '../../utils/temperatureFanPresets';
import { tilePrimaryActions, type TilePrimaryAction } from '../../utils/touchActions';
import { maintenanceHoldOf, stillWanted } from '../../utils/touchOrder';
import { HMSErrorModal } from '../HMSErrorModal';
import { InfoHint } from '../ui/InfoHint';
import { HoldChip } from '../HoldChip';
import { TONE_CLASS } from '../holdChipTone';
import { PrinterActionPrompts } from '../PrinterActionPrompts';
import { TouchMoreSheet } from './TouchMoreSheet';

export interface TouchTileProps {
  printer: Printer;
  status: PrinterStatus | undefined;
  requirePlateClear: boolean;
  fanPresets: PresetTriple;
  dateFormat: DateFormat;
  timeFormat: TimeFormat;
  onOpenCamera: (printerId: number) => void;
}

// A contextual button is as wide as its one-line label (never wrapped, never cut): two
// share the row when both fit, and the second moves to its own row when they do not.
// More is the 56 px square in the header, so a label always has the whole row.
const ACTION =
  'flex min-h-[56px] max-w-full flex-auto items-center justify-center gap-2 whitespace-nowrap rounded-xl px-3 text-sm font-semibold leading-tight transition-colors disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green focus-visible:ring-offset-2 focus-visible:ring-offset-bambu-dark-secondary';

/** Tone of each contextual button: the verb's own colour family on the card. */
const ACTION_TONE: Record<TilePrimaryAction, string> = {
  ignoreResume: 'bg-amber-500/20 text-amber-200 hover:bg-amber-500/30',
  stop: 'bg-red-500/20 text-red-300 hover:bg-red-500/30',
  exitMaintenance: 'bg-amber-500/20 text-amber-200 hover:bg-amber-500/30',
  resume: 'bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30',
  recover: 'bg-red-500/20 text-red-200 hover:bg-red-500/30',
  markCleared: 'bg-yellow-500/20 text-yellow-300 hover:bg-yellow-500/30',
  camera: 'bg-bambu-dark text-white hover:bg-bambu-dark-tertiary',
};

export function TouchTile({
  printer,
  status,
  requirePlateClear,
  fanPresets,
  dateFormat,
  timeFormat,
  onOpenCamera,
}: TouchTileProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const actions = usePrinterActions(printer, status, timeFormat);
  const [showMore, setShowMore] = useState(false);
  const [showMessages, setShowMessages] = useState(false);
  const entry = { printer, status };
  const hold = maintenanceHoldOf(entry);
  const canViewCamera = hasPermission('camera:view');
  const wantsStill = stillWanted(entry);
  const still = useCameraStill(printer.id, wantsStill && canViewCamera);
  const nameId = `touch-tile-${printer.id}-name`;

  const state = status?.state ?? null;
  const isPrinting = state === 'RUNNING' || state === 'PAUSE';
  const chip = status?.hold_chip ?? null;
  const bucket = classifyPrinterStatus(status);
  const stateWords = status?.connected && status.stg_cur_name ? status.stg_cur_name : t(STATUS_GROUP_META[bucket].labelKey);
  const plate = plateStatusOf(status, requirePlateClear);
  const primary = tilePrimaryActions(printer, status);
  const edgeClass = chip ? TONE_CLASS[chip.tone] : state === 'RUNNING' ? 'text-bambu-green' : 'text-bambu-dark-tertiary';
  const releaseAt = hold?.release_at ? parseUTCDate(hold.release_at) : null;
  const printName = isPrinting
    ? formatPrintName(status?.subtask_name || status?.current_print || null, status?.gcode_file, t)
    : '';
  const capturedAt = still.data?.capturedAt ? parseUTCDate(still.data.capturedAt) : null;
  const openCamera = () => onOpenCamera(printer.id);

  const primaryButton = (action: TilePrimaryAction) => {
    const view = {
      ignoreResume: {
        label: t('printers.plateCheck.ignoreResume'),
        Icon: SkipForward,
        disabled: actions.controlBusy || !hasPermission('printers:control'),
        run: () => {
          const exit = status?.plate_check_exit;
          if (exit) actions.ask({ kind: 'ignoreResume', exit });
        },
      },
      stop: {
        label: t('printers.stop'),
        Icon: Square,
        disabled: !isPrinting || actions.controlBusy || !hasPermission('printers:control'),
        run: () => actions.ask({ kind: 'stop' }),
      },
      exitMaintenance: {
        label: t('printers.maintenance.exitButton'),
        Icon: Wrench,
        disabled: actions.exitMaintenance.isPending || !hasPermission('printers:control'),
        run: () => actions.exitMaintenance.mutate(),
      },
      resume: {
        label: t('printers.resume'),
        Icon: Play,
        disabled: actions.controlBusy || !hasPermission('printers:control'),
        run: () => actions.ask({ kind: 'resume' }),
      },
      recover: {
        label: t('printers.quarantine.recover'),
        Icon: RotateCw,
        disabled: actions.recover.isPending || !hasPermission('printers:recover'),
        run: () => actions.ask({ kind: 'recover' }),
      },
      markCleared: {
        label: t('printers.quarantine.markPlateCleared'),
        Icon: CheckCircle2,
        disabled: actions.clearPlate.isPending || !hasPermission('printers:clear_plate'),
        run: () => actions.clearPlate.mutate(),
      },
      camera: {
        label: t('printers.camera'),
        Icon: Camera,
        disabled: !canViewCamera,
        run: openCamera,
      },
    }[action];
    const { Icon } = view;
    return (
      <button
        key={action}
        type="button"
        data-action={action}
        className={`${ACTION} ${ACTION_TONE[action]}`}
        disabled={view.disabled}
        onClick={view.run}
      >
        <Icon className="h-5 w-5 flex-shrink-0" aria-hidden="true" />
        <span>{view.label}</span>
      </button>
    );
  };

  return (
    <article
      aria-labelledby={nameId}
      className="relative flex min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary pl-1.5"
    >
      <span
        aria-hidden="true"
        data-testid="touch-tile-edge"
        className={`absolute inset-y-0 left-0 w-1.5 border-l-[6px] border-current ${edgeClass}`}
      />

      <header className="flex items-start justify-between gap-2 px-3 pt-3">
        <div className="min-w-0">
          <h2 id={nameId} className="truncate text-lg font-semibold text-white">{printer.name}</h2>
          <p className="truncate text-sm text-bambu-gray">{stateWords}</p>
        </div>
        <button
          type="button"
          data-testid="touch-tile-more"
          className="flex h-[56px] w-[56px] flex-none items-center justify-center rounded-xl bg-bambu-dark text-white transition-colors hover:bg-bambu-dark-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green focus-visible:ring-offset-2 focus-visible:ring-offset-bambu-dark-secondary"
          onClick={() => setShowMore(true)}
          aria-haspopup="dialog"
          aria-label={t('touch.more')}
        >
          <MoreHorizontal className="h-6 w-6" aria-hidden="true" />
        </button>
      </header>

      {(plate || chip || hold || printer.quarantined) && (
        <div className="flex flex-wrap items-center gap-2 px-3 pt-2">
          {plate && (
            <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${plate.className}`}>
              {t(plate.label.key, plate.label.values)}
            </span>
          )}
          {chip && <HoldChip chip={chip} touch />}
          {printer.quarantined && (
            <span className="inline-flex items-center gap-1 rounded-full bg-status-error/20 px-2 py-1 text-sm text-status-error">
              {t('printers.quarantine.badge')}
              {printer.quarantine_reason && <InfoHint text={printer.quarantine_reason} touch />}
            </span>
          )}
          {hold && (
            <span className="inline-flex items-center gap-1 rounded-full bg-amber-500/20 px-2 py-1 text-xs font-medium text-amber-200">
              <Wrench className="h-3 w-3" aria-hidden="true" />
              {releaseAt
                ? t('printers.maintenance.until', { time: formatDateTime(hold.release_at ?? '', timeFormat) })
                : t('printers.maintenance.badge')}
            </span>
          )}
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col px-3 py-2">
        {wantsStill ? (
          <button
            type="button"
            onClick={openCamera}
            disabled={!canViewCamera}
            aria-label={t('touch.openCamera', { name: printer.name })}
            className="relative min-h-0 flex-1 overflow-hidden rounded-lg bg-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
          >
            {still.data ? (
              <>
                <img src={still.data.src} alt="" className="absolute inset-0 h-full w-full object-cover" />
                {capturedAt && (
                  <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1.5 py-0.5 text-xs text-white">
                    {t('touch.stillAt', { time: formatTimeOnly(capturedAt, timeFormat) })}
                  </span>
                )}
              </>
            ) : (
              <span className="flex h-full items-center justify-center gap-2 text-sm text-bambu-gray">
                <CameraOff className="h-5 w-5" aria-hidden="true" />
                {still.isPending && canViewCamera ? t('touch.stillLoading') : t('touch.stillNone')}
              </span>
            )}
          </button>
        ) : isPrinting ? (
          <div className="flex min-h-0 flex-1 flex-col justify-center gap-2">
            {printName && <p className="truncate text-sm text-white">{printName}</p>}
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-3xl font-semibold tabular-nums text-white">{Math.round(status?.progress ?? 0)} %</span>
              {status?.remaining_time != null && status.remaining_time > 0 && (
                <span className="text-sm text-bambu-gray">
                  {t('touch.finishAt', { time: formatETA(status.remaining_time, timeFormat, t) })}
                </span>
              )}
            </div>
            <div
              role="progressbar"
              aria-labelledby={nameId}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(status?.progress ?? 0)}
              className="h-3 overflow-hidden rounded-full bg-bambu-dark"
            >
              <div
                className={`h-full rounded-full ${state === 'PAUSE' ? 'bg-yellow-400' : 'bg-bambu-green'}`}
                style={{ width: `${Math.min(100, Math.max(0, status?.progress ?? 0))}%` }}
              />
            </div>
            {status?.layer_num != null && status.total_layers != null && status.total_layers > 0 && (
              <p className="text-sm text-bambu-gray">
                {t('touch.layer', { cur: status.layer_num, total: status.total_layers })}
              </p>
            )}
          </div>
        ) : (
          <div className="flex-1" />
        )}
      </div>

      {primary.length > 0 && (
        <div className="flex flex-wrap gap-3 px-3 pb-3" data-testid="touch-tile-actions">
          {primary.map(primaryButton)}
        </div>
      )}

      {showMore && (
        <TouchMoreSheet
          printer={printer}
          status={status}
          actions={actions}
          hold={hold}
          fanPresets={fanPresets}
          dateFormat={dateFormat}
          timeFormat={timeFormat}
          onOpenCamera={openCamera}
          onOpenMessages={() => setShowMessages(true)}
          onClose={() => setShowMore(false)}
        />
      )}
      {showMessages && (
        <HMSErrorModal
          printerName={printer.name}
          errors={status?.hms_errors ?? []}
          onClose={() => setShowMessages(false)}
          printerId={printer.id}
          hasPermission={hasPermission}
          touch
        />
      )}
      <PrinterActionPrompts
        printer={printer}
        actions={actions}
        dateFormat={dateFormat}
        timeFormat={timeFormat}
        touch
      />
    </article>
  );
}
