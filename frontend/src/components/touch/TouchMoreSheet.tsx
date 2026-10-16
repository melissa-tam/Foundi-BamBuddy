/**
 * The touch tile's More sheet: every verb the tile's two contextual buttons leave out —
 * Stop, Pause / Resume, Eject plate, maintenance mode (on: the shared enter dialog with
 * its return time; off: the shared maintenance banner with Exit and the return time),
 * the printer's HMS messages (the existing modal), fan presets, the chamber light and
 * the live camera. The verbs are `usePrinterActions`'; this sheet only lays them out at
 * touch size (64 px rows, 12 px gaps).
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Camera, Fan, MessageSquareWarning, Pause, Play, Square, Wind, Wrench, X } from 'lucide-react';
import type { Printer, PrinterStatus, ServiceHoldState } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { jobOwnsPlate } from '../../hooks/useEjectPlate';
import type { FanKey, PrinterActions } from '../../hooks/usePrinterActions';
import type { DateFormat, TimeFormat } from '../../utils/date';
import type { PresetTriple } from '../../utils/temperatureFanPresets';
import { ChamberLight } from '../icons/ChamberLight';
import { MaintenanceBanner } from '../maintenance/MaintenanceBanner';
import { Modal } from '../ui/Modal';

interface TouchMoreSheetProps {
  printer: Printer;
  status: PrinterStatus | undefined;
  actions: PrinterActions;
  hold: ServiceHoldState | null;
  fanPresets: PresetTriple;
  dateFormat: DateFormat;
  timeFormat: TimeFormat;
  onOpenCamera: () => void;
  onOpenMessages: () => void;
  onClose: () => void;
}

const ROW =
  'flex min-h-[64px] w-full items-center gap-3 rounded-xl bg-bambu-dark px-4 text-left text-base font-medium text-white transition-colors hover:bg-bambu-dark-tertiary disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green';
const PRESET =
  'min-h-[56px] flex-1 rounded-lg border px-2 text-base font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green';

export function TouchMoreSheet({
  printer,
  status,
  actions,
  hold,
  fanPresets,
  dateFormat,
  timeFormat,
  onOpenCamera,
  onOpenMessages,
  onClose,
}: TouchMoreSheetProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const titleId = useId();
  const canControl = hasPermission('printers:control');
  const connected = status?.connected === true;
  const state = status?.state ?? null;
  const isPrinting = state === 'RUNNING' || state === 'PAUSE';
  const messageCount = status?.hms_errors?.length ?? 0;

  /** Close the sheet first, so the prompt the verb opens is the only dialog on screen. */
  const then = (run: () => void) => () => {
    onClose();
    run();
  };

  const fans: { key: FanKey; label: string; value: number }[] = [
    { key: 'part', label: t('printers.fans.partCooling'), value: status?.cooling_fan_speed ?? 0 },
    { key: 'aux', label: t('printers.fans.auxiliary'), value: status?.big_fan1_speed ?? 0 },
    ...(status?.has_chamber_fan === true
      ? [{ key: 'chamber' as const, label: t('printers.fans.chamber'), value: status.big_fan2_speed ?? 0 }]
      : []),
  ];
  const presets = [0, ...fanPresets];

  return (
    <Modal onClose={onClose} labelledBy={titleId} widthClass="max-w-4xl" className="p-5">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 id={titleId} className="truncate text-xl font-semibold text-white">{printer.name}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label={t('common.close')}
          className="flex min-h-[56px] min-w-[56px] items-center justify-center rounded-xl text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
        >
          <X className="h-6 w-6" aria-hidden="true" />
        </button>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <button
          type="button"
          className={ROW}
          disabled={!isPrinting || actions.controlBusy || !canControl}
          onClick={then(() => actions.ask({ kind: 'stop' }))}
        >
          <Square className="h-5 w-5 text-red-400" aria-hidden="true" />
          {t('printers.stop')}
        </button>
        {state === 'PAUSE' ? (
          <button
            type="button"
            className={ROW}
            disabled={actions.controlBusy || !canControl}
            onClick={then(() => actions.ask({ kind: 'resume' }))}
          >
            <Play className="h-5 w-5 text-bambu-green" aria-hidden="true" />
            {t('printers.resume')}
          </button>
        ) : (
          <button
            type="button"
            className={ROW}
            disabled={state !== 'RUNNING' || actions.controlBusy || !canControl}
            onClick={then(() => actions.ask({ kind: 'pause' }))}
          >
            <Pause className="h-5 w-5 text-yellow-400" aria-hidden="true" />
            {t('printers.pause')}
          </button>
        )}
        <button
          type="button"
          className={ROW}
          disabled={!connected || jobOwnsPlate(state) || actions.eject.isPending || !canControl}
          onClick={then(() => actions.eject.eject({ declareOccupied: true }))}
        >
          <Wind className="h-5 w-5 text-blue-400" aria-hidden="true" />
          {t('printers.eject.action')}
        </button>
        {!hold && (
          <button
            type="button"
            className={ROW}
            disabled={actions.enterMaintenance.isPending || !canControl}
            onClick={then(() => actions.ask({ kind: 'enterMaintenance' }))}
          >
            <Wrench className="h-5 w-5 text-amber-400" aria-hidden="true" />
            {t('printers.maintenance.menuEnter')}
          </button>
        )}
        <button
          type="button"
          className={ROW}
          disabled={messageCount === 0}
          onClick={then(onOpenMessages)}
        >
          <MessageSquareWarning className="h-5 w-5 text-orange-400" aria-hidden="true" />
          {t('touch.printerMessages', { count: messageCount })}
        </button>
        <button
          type="button"
          className={ROW}
          aria-pressed={status?.chamber_light === true}
          disabled={!connected || actions.chamberLight.isPending || !canControl}
          onClick={() => actions.chamberLight.mutate(!(status?.chamber_light === true))}
        >
          <ChamberLight on={status?.chamber_light === true} className="h-5 w-5" />
          {status?.chamber_light ? t('printers.chamberLightOff') : t('printers.chamberLightOn')}
        </button>
        <button
          type="button"
          className={ROW}
          disabled={!hasPermission('camera:view')}
          onClick={then(onOpenCamera)}
        >
          <Camera className="h-5 w-5 text-bambu-gray" aria-hidden="true" />
          {t('touch.liveCamera')}
        </button>
      </div>

      {hold && (
        <div className="mt-4">
          <MaintenanceBanner
            printerId={printer.id}
            printerName={printer.name}
            hold={hold}
            canControl={canControl}
            exitPending={actions.exitMaintenance.isPending}
            onExit={() => actions.exitMaintenance.mutate(undefined, { onSuccess: onClose })}
            reschedulePending={actions.rescheduleMaintenance.isPending}
            onReschedule={(releaseAt) => actions.rescheduleMaintenance.mutateAsync(releaseAt)}
            dateFormat={dateFormat}
            timeFormat={timeFormat}
            touch
          />
        </div>
      )}

      <section className="mt-4" aria-labelledby={`${titleId}-fans`}>
        <h3 id={`${titleId}-fans`} className="mb-2 flex items-center gap-2 text-base font-semibold text-white">
          <Fan className="h-5 w-5 text-cyan-400" aria-hidden="true" />
          {t('touch.fans')}
        </h3>
        <div className="space-y-3">
          {fans.map((fan) => (
            <div key={fan.key} role="group" aria-label={fan.label} className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-3">
              <p className="text-sm text-bambu-gray sm:w-48 sm:flex-shrink-0">
                {fan.label}: {fan.value} %
              </p>
              <div className="flex flex-1 gap-3">
                {presets.map((speed) => {
                  const current = fan.value === speed;
                  return (
                    <button
                      key={speed}
                      type="button"
                      aria-pressed={current}
                      disabled={!connected || actions.fanSpeed.isPending || !canControl}
                      onClick={() => actions.fanSpeed.mutate({ fan: fan.key, speed })}
                      className={`${PRESET} ${
                        current
                          ? 'border-bambu-green bg-bambu-green/20 text-bambu-green'
                          : 'border-bambu-dark-tertiary bg-bambu-dark text-white hover:bg-bambu-dark-tertiary'
                      }`}
                    >
                      {speed === 0 ? t('touch.fanOff') : `${speed} %`}
                    </button>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </section>
    </Modal>
  );
}
