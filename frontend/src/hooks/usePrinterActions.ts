/**
 * THE printer verbs the Printers page card and the touch view's tile both offer, and
 * the confirm prompts in front of them — one implementation each (the card's inline
 * mutations moved here; `components/PrinterActionPrompts` renders the prompts).
 *
 * Verbs: stop, pause, resume, the plate-check "Ignore and resume" press, mark plate
 * cleared, Recover, eject (the `useEjectPlate` flow), enter / exit maintenance mode and
 * its return time, fan presets and the chamber light. Every other card verb (AMS, jog,
 * temperatures, drying, …) stays on the card: only the touch view shares these.
 * Other surfaces with their own copy of a verb (SpoolBuddy's clear plate, the camera
 * viewers' chamber light) keep it — they are allowlisted by name in
 * `__tests__/hooks/usePrinterActionsOwnership.test.ts`.
 *
 * Each verb's copy (toasts, refusal routing) is what the card always said; a caller
 * adds its own UI reaction through TanStack's per-call `mutate(vars, { onSuccess })`.
 */
import { useState } from 'react';
import { queryOptions, useMutation, useQueryClient, type UseMutationResult } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  api,
  ApiError,
  type ClearPlateResult,
  type HmsActionResult,
  type PlateCheckAction,
  type PlateCheckExit,
  type Printer,
  type PrinterStatus,
  type PrintResumeResponse,
  type RecoverResult,
  type ServiceHoldEnterResult,
  type ServiceHoldExitResult,
  type ServiceHoldRescheduleResult,
} from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { formatDateTime, type TimeFormat } from '../utils/date';
import { refillingToast, refusalToast, resumeRefusalOf } from '../utils/printResume';
import { inFlightEject, recoverApplies, recoverEffects, type RecoverEffect } from '../utils/printerRecovery';
import { useEjectPlate } from './useEjectPlate';

/** A confirm prompt in front of a verb; one at a time per printer. */
export type PrinterPrompt =
  | { kind: 'stop' }
  | { kind: 'pause' }
  | { kind: 'resume' }
  /** The plate-check dialog captured at the click: the confirm sends THAT dialog. */
  | { kind: 'ignoreResume'; exit: PlateCheckExit }
  | { kind: 'recover' }
  | { kind: 'enterMaintenance' };

export type FanKey = 'part' | 'aux' | 'chamber';

/** The status field each fan's speed is reported on (the optimistic write). */
const FAN_FIELD: Record<FanKey, 'cooling_fan_speed' | 'big_fan1_speed' | 'big_fan2_speed'> = {
  part: 'cooling_fan_speed',
  aux: 'big_fan1_speed',
  chamber: 'big_fan2_speed',
};

type CommandAnswer = { success: boolean; message: string };
interface StatusSnapshot {
  previousStatus: PrinterStatus | undefined;
}

export interface PrinterActions {
  stop: UseMutationResult<CommandAnswer, Error, void>;
  pause: UseMutationResult<CommandAnswer, Error, void>;
  resume: UseMutationResult<PrintResumeResponse, Error, void>;
  ignoreResume: UseMutationResult<HmsActionResult, Error, PlateCheckExit>;
  clearPlate: UseMutationResult<ClearPlateResult, Error, void>;
  recover: UseMutationResult<RecoverResult, Error, void>;
  fanSpeed: UseMutationResult<CommandAnswer, Error, { fan: FanKey; speed: number }, StatusSnapshot>;
  chamberLight: UseMutationResult<CommandAnswer, Error, boolean, StatusSnapshot>;
  /** `releaseAt`: the optional scheduled return to service (UTC ISO), null = Manual. */
  enterMaintenance: UseMutationResult<ServiceHoldEnterResult, Error, string | null>;
  exitMaintenance: UseMutationResult<ServiceHoldExitResult, Error, void>;
  /** Set, change or clear the hold's return to service (422 on a time not in the future). */
  rescheduleMaintenance: UseMutationResult<ServiceHoldRescheduleResult, Error, string | null>;
  /** The one eject flow (mutation, hot-bed confirm, eject dialog, refusal routing). */
  eject: ReturnType<typeof useEjectPlate>;
  /** The prompt open on this printer, or null. */
  prompt: PrinterPrompt | null;
  ask: (prompt: PrinterPrompt) => void;
  dismiss: () => void;
  /** The card's door: confirm only when entering would stop something, else enter now. */
  requestEnterMaintenance: () => void;
  /** One line per live farm action entering maintenance stops — the confirm's body. */
  maintenanceEffects: string[];
  /** What Recover would clear here (`utils/printerRecovery`). */
  recoverEffects: RecoverEffect[];
  canRecover: boolean;
  /** A print-control verb is in flight (stop, pause, resume, ignore and resume). */
  controlBusy: boolean;
}

/** The printing queue unit(s) on a printer — the stop confirm names a farm stop from it. */
export function printingQueueQuery(printerId: number, enabled: boolean) {
  return queryOptions({
    queryKey: ['queue', printerId, 'printing'],
    queryFn: () => api.getQueue(printerId, 'printing'),
    enabled,
  });
}

export function usePrinterActions(
  printer: Printer,
  status: PrinterStatus | undefined,
  timeFormat: TimeFormat = 'system',
): PrinterActions {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [prompt, setPrompt] = useState<PrinterPrompt | null>(null);
  const eject = useEjectPlate(printer.id);
  const statusKey = ['printerStatus', printer.id] as const;
  const failed = (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error');
  const refreshStatus = () => queryClient.invalidateQueries({ queryKey: statusKey });

  const stop = useMutation({
    mutationFn: () => api.stopPrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printStopped'));
      refreshStatus();
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToStopPrint'), 'error'),
  });

  const pause = useMutation({
    mutationFn: () => api.pausePrint(printer.id),
    onSuccess: () => {
      showToast(t('printers.toast.printPaused'));
      refreshStatus();
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToPausePrint'), 'error'),
  });

  // Over an empty toolhead the backend refills it first and answers `refilling` at
  // once; a refusal is a typed 409 (`utils/printResume` owns all three sentences).
  const resume = useMutation({
    mutationFn: () => api.resumePrint(printer.id),
    onSuccess: (answer) => {
      if (answer.status === 'refilling') {
        const toast = refillingToast(answer.slot, t);
        showToast(toast.message, toast.type);
      } else {
        showToast(t('printers.toast.printResumed'));
      }
      refreshStatus();
    },
    onError: (error: Error) => {
      const refusal = resumeRefusalOf(error);
      if (refusal !== null) {
        const toast = refusalToast(refusal, t);
        showToast(toast.message, toast.type);
      } else {
        showToast(error.message || t('printers.toast.failedToResumePrint'), 'error');
      }
    },
  });

  // The printer's own plate-check "Ignore and resume" button. A refusal (409: the farm
  // is still acting, or no paused plate check) arrives as the backend's `detail`.
  const ignoreResume = useMutation({
    mutationFn: (exit: PlateCheckExit) =>
      api.executeHMSAction(printer.id, {
        action: 'IGNORE_RESUME' satisfies PlateCheckAction,
        print_error: exit.print_error,
        job_id: exit.job_id,
      }),
    onSuccess: () => {
      showToast(t('printers.plateCheck.sent'));
      refreshStatus();
    },
    onError: (error: Error) => showToast(t('printers.plateCheck.refused', { message: error.message }), 'error'),
  });

  const clearPlate = useMutation({
    mutationFn: () => api.clearPlate(printer.id),
    onSuccess: (result) => {
      // One message per event: the gate release, plus the fault it closed.
      const closedFault = result.incidents_closed.length > 0
        ? ` ${t('printers.plateStatus.clearedClosedFault')}`
        : '';
      showToast(t('queue.clearPlateSuccess') + closedFault);
      queryClient.setQueryData(statusKey, (old: PrinterStatus | undefined) =>
        old ? { ...old, awaiting_plate_clear: false } : old
      );
      refreshStatus();
      queryClient.invalidateQueries({ queryKey: ['queue', printer.id] });
    },
    // Refused while an eject owns the printer (the sweep's own terminal clears the
    // gate) — that refusal gets real copy, not backend English.
    onError: (error: Error) =>
      showToast(
        error instanceof ApiError && error.code === 'eject_in_flight'
          ? t('printers.plateStatus.ejectInFlight')
          : error.message || t('printers.toast.failedToSendCommand'),
        'error'
      ),
  });

  // Farm one-click recovery: clears the plate hold, lifts quarantine, and resumes any
  // paused production run on this printer in a single action.
  const recover = useMutation({
    mutationFn: () => api.recoverPrinter(printer.id),
    onSuccess: (result) => {
      const closedFault = result.incidents_closed.length > 0
        ? ` ${t('printers.quarantine.recoverClosedFault')}`
        : '';
      showToast(
        t('printers.quarantine.recoverSuccess', { count: result.runs_resumed.length }) + closedFault
      );
      setPrompt(null);
      queryClient.setQueryData(statusKey, (old: PrinterStatus | undefined) =>
        old ? { ...old, awaiting_plate_clear: false } : old
      );
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      refreshStatus();
      queryClient.invalidateQueries({ queryKey: ['queue', printer.id] });
      queryClient.invalidateQueries({ queryKey: ['production-runs'] });
    },
    onError: (error: Error) => showToast(error.message || t('printers.quarantine.recoverError'), 'error'),
  });

  const fanSpeed = useMutation<CommandAnswer, Error, { fan: FanKey; speed: number }, StatusSnapshot>({
    mutationFn: ({ fan, speed }) => api.setFanSpeed(printer.id, fan, speed),
    onMutate: async ({ fan, speed }) => {
      await queryClient.cancelQueries({ queryKey: statusKey });
      const previousStatus = queryClient.getQueryData<PrinterStatus>(statusKey);
      queryClient.setQueryData(statusKey, (old: PrinterStatus | undefined) =>
        old ? { ...old, [FAN_FIELD[fan]]: speed } : old
      );
      return { previousStatus };
    },
    onSuccess: (result) => {
      showToast(result.message);
      refreshStatus();
    },
    onError: (error, _variables, context) => {
      if (context?.previousStatus) queryClient.setQueryData(statusKey, context.previousStatus);
      failed(error);
    },
  });

  const chamberLight = useMutation<CommandAnswer, Error, boolean, StatusSnapshot>({
    mutationFn: (on) => api.setChamberLight(printer.id, on),
    onMutate: async (on) => {
      await queryClient.cancelQueries({ queryKey: statusKey });
      const previousStatus = queryClient.getQueryData<PrinterStatus>(statusKey);
      queryClient.setQueryData(statusKey, (old: PrinterStatus | undefined) =>
        old ? { ...old, chamber_light: on } : old
      );
      return { previousStatus };
    },
    onSuccess: (_answer, on) => {
      showToast(`Chamber light ${on ? 'on' : 'off'}`);
    },
    onError: (error, _on, context) => {
      if (context?.previousStatus) queryClient.setQueryData(statusKey, context.previousStatus);
      showToast(error.message || t('printers.toast.failedToControlChamberLight'), 'error');
    },
  });

  // Maintenance mode = the service hold. The session and every manual verb stay up;
  // the farm takes the printer out of dispatch, auto-eject, cooldown and the recovery
  // drivers. Entering quiesces whatever is live first.
  const enterMaintenance = useMutation({
    mutationFn: (releaseAt: string | null) => api.enterServiceHold(printer.id, releaseAt),
    onSuccess: () => {
      setPrompt(null);
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      refreshStatus();
      showToast(t('printers.maintenance.toastEntered', { name: printer.name }), 'success');
    },
    onError: failed,
  });

  const exitMaintenance = useMutation({
    mutationFn: () => api.exitServiceHold(printer.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      refreshStatus();
      showToast(t('printers.maintenance.toastExited', { name: printer.name }), 'success');
    },
    onError: failed,
  });

  const rescheduleMaintenance = useMutation({
    mutationFn: (releaseAt: string | null) => api.rescheduleServiceHold(printer.id, releaseAt),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      refreshStatus();
      showToast(
        result.release_at
          ? t('printers.maintenance.toastReturnSet', {
              name: printer.name,
              time: formatDateTime(result.release_at, timeFormat),
            })
          : t('printers.maintenance.toastReturnCleared', { name: printer.name }),
        'success',
      );
    },
    onError: failed,
  });

  // What entering the hold will stop, read off live status. A RUNNING print is NOT on
  // it: no mode verb ends a print (only Stop does).
  const maintenanceEffects: string[] = [];
  if (inFlightEject(status)) maintenanceEffects.push(t('printers.maintenance.confirmEffectEject'));
  if (status?.eject_watch) maintenanceEffects.push(t('printers.maintenance.confirmEffectCooldown'));

  const effects = recoverEffects(printer, status);

  return {
    stop,
    pause,
    resume,
    ignoreResume,
    clearPlate,
    recover,
    fanSpeed,
    chamberLight,
    enterMaintenance,
    exitMaintenance,
    rescheduleMaintenance,
    eject,
    prompt,
    ask: setPrompt,
    dismiss: () => setPrompt(null),
    requestEnterMaintenance: () => {
      if (maintenanceEffects.length > 0) setPrompt({ kind: 'enterMaintenance' });
      else enterMaintenance.mutate(null);
    },
    maintenanceEffects,
    recoverEffects: effects,
    canRecover: recoverApplies(effects),
    controlBusy: stop.isPending || pause.isPending || resume.isPending || ignoreResume.isPending,
  };
}
