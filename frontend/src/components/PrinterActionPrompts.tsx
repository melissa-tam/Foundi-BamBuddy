/**
 * The confirm prompts in front of the shared printer verbs (`hooks/usePrinterActions`):
 * Stop, Pause, Resume, the plate-check "Ignore and resume", Recover, entering
 * maintenance mode, and the eject flow's hot-bed confirm and eject dialog. ONE copy,
 * rendered by the Printers page card and the touch view's tile; `touch` sizes the same
 * dialogs up for a touchscreen (never a fork).
 *
 * Each prompt's consequence lives here, never on the surface that opened it.
 */
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import type { Printer } from '../api/client';
import { printingQueueQuery, type PrinterActions } from '../hooks/usePrinterActions';
import { formatTimeOnly, parseUTCDate, type DateFormat, type TimeFormat } from '../utils/date';
import type { RecoverEffect } from '../utils/printerRecovery';
import { ConfirmModal } from './ConfirmModal';
import { EjectPlateDialog } from './EjectPlateDialog';
import { EnterMaintenanceDialog } from './maintenance/EnterMaintenanceDialog';

// One line per Recover effect in the confirm dialog: the operator confirms an explicit
// override, so the dialog states exactly what THIS printer loses — no fixed list that
// promises effects the printer does not have. The verb's own unconditional consequence
// (resuming the paused runs) is appended after these; it belongs to no printer state.
const RECOVER_EFFECT_COPY: Record<RecoverEffect, string> = {
  plate: 'printers.quarantine.recoverEffectPlate',
  lease: 'printers.quarantine.recoverEffectLease',
  eject: 'printers.quarantine.recoverEffectEject',
  quarantine: 'printers.quarantine.recoverEffectQuarantine',
  equipment_fault: 'printers.quarantine.recoverEffectFault',
};

interface PrinterActionPromptsProps {
  printer: Printer;
  actions: PrinterActions;
  dateFormat?: DateFormat;
  timeFormat?: TimeFormat;
  /** Touch sizing (the touch view). */
  touch?: boolean;
}

export function PrinterActionPrompts({
  printer,
  actions,
  dateFormat = 'system',
  timeFormat = 'system',
  touch = false,
}: PrinterActionPromptsProps) {
  const { t } = useTranslation();
  const { prompt, dismiss, eject } = actions;
  // The stop confirm names the farm consequence when the printing unit belongs to a
  // production run. Shared cache with the card's own read of the same unit.
  const { data: printingQueueItems } = useQuery(printingQueueQuery(printer.id, prompt?.kind === 'stop'));

  return (
    <>
      {prompt?.kind === 'stop' && (
        <ConfirmModal
          title={t('printers.confirm.stopTitle')}
          message={
            printingQueueItems?.[0]?.production_run_id != null
              ? `${t('printers.confirm.stopMessage', { name: printer.name })}\n\n${t('queue.farm.stopConsequence')}`
              : t('printers.confirm.stopMessage', { name: printer.name })
          }
          confirmText={t('printers.confirm.stopButton')}
          variant="danger"
          touch={touch}
          onConfirm={() => {
            actions.stop.mutate();
            dismiss();
          }}
          onCancel={dismiss}
        />
      )}

      {prompt?.kind === 'pause' && (
        <ConfirmModal
          title={t('printers.confirm.pauseTitle')}
          message={t('printers.confirm.pauseMessage', { name: printer.name })}
          confirmText={t('printers.confirm.pauseButton')}
          variant="default"
          touch={touch}
          onConfirm={() => {
            actions.pause.mutate();
            dismiss();
          }}
          onCancel={dismiss}
        />
      )}

      {prompt?.kind === 'resume' && (
        <ConfirmModal
          title={t('printers.confirm.resumeTitle')}
          message={t('printers.confirm.resumeMessage', { name: printer.name })}
          confirmText={t('printers.confirm.resumeButton')}
          variant="default"
          touch={touch}
          onConfirm={() => {
            actions.resume.mutate();
            dismiss();
          }}
          onCancel={dismiss}
        />
      )}

      {/* The deadline line names when the farm stops the print if the window runs out. */}
      {prompt?.kind === 'ignoreResume' && (() => {
        const deadline = parseUTCDate(prompt.exit.deadline_at);
        const body = t('printers.plateCheck.confirmBody');
        return (
          <ConfirmModal
            title={t('printers.plateCheck.confirmTitle')}
            message={
              deadline
                ? `${body}\n\n${t('printers.plateCheck.confirmDeadline', { time: formatTimeOnly(deadline, timeFormat) })}`
                : body
            }
            confirmText={t('printers.plateCheck.ignoreResume')}
            variant="warning"
            touch={touch}
            onConfirm={() => {
              actions.ignoreResume.mutate(prompt.exit);
              dismiss();
            }}
            onCancel={dismiss}
          />
        );
      })()}

      {prompt?.kind === 'recover' && (
        <ConfirmModal
          title={t('printers.quarantine.recoverTitle')}
          message={[
            printer.quarantine_reason
              ? t('printers.quarantine.recoverMessageWithReason', { name: printer.name, reason: printer.quarantine_reason })
              : t('printers.quarantine.recoverMessage', { name: printer.name }),
            [
              ...actions.recoverEffects.map((effect) => t(RECOVER_EFFECT_COPY[effect])),
              t('printers.quarantine.recoverEffectResume'),
            ].join('\n'),
          ].join('\n\n')}
          confirmText={t('printers.quarantine.recover')}
          variant="warning"
          touch={touch}
          isLoading={actions.recover.isPending}
          onConfirm={() => actions.recover.mutate()}
          onCancel={dismiss}
        />
      )}

      {/* The body is what the hold will stop plus the optional return to service. */}
      {prompt?.kind === 'enterMaintenance' && (
        <EnterMaintenanceDialog
          printerId={printer.id}
          printerName={printer.name}
          effects={actions.maintenanceEffects}
          pending={actions.enterMaintenance.isPending}
          onConfirm={(releaseAt) => actions.enterMaintenance.mutate(releaseAt)}
          onCancel={dismiss}
          dateFormat={dateFormat}
          timeFormat={timeFormat}
          touch={touch}
        />
      )}

      {/* Hot-bed eject: the backend refused because the bed is above the release line
          (or nothing says it is cool enough); confirming re-calls with allow_hot. */}
      {eject.hotConfirm && (
        <ConfirmModal
          title={t('printers.eject.confirmTitle')}
          message={
            eject.hotConfirm.thresholdC === null
              ? t('printers.eject.confirmBodyNoLine', { bed: Math.round(eject.hotConfirm.bedC) })
              : t('printers.eject.confirmBody', {
                  bed: Math.round(eject.hotConfirm.bedC),
                  threshold: Math.round(eject.hotConfirm.thresholdC),
                })
          }
          confirmText={t('printers.eject.confirm')}
          cancelText={t('printers.eject.cancel')}
          variant="danger"
          touch={touch}
          isLoading={eject.isPending}
          onConfirm={eject.confirmHot}
          onCancel={eject.closeHotConfirm}
        />
      )}

      {/* The one eject dialog: every sweep that needs operator input routes here. */}
      {eject.dialog && (
        <EjectPlateDialog
          printerId={printer.id}
          dialog={eject.dialog}
          ejectProfiles={eject.ejectProfiles}
          selectedProfileId={eject.selectedProfileId}
          onSelectProfile={eject.setSelectedProfileId}
          heightInput={eject.heightInput}
          onHeightChange={eject.setHeightInput}
          heightValid={eject.heightValid}
          error={eject.dialogError}
          isPending={eject.isPending}
          onConfirm={eject.confirmDialog}
          onClose={eject.closeDialog}
          touch={touch}
        />
      )}
    </>
  );
}
