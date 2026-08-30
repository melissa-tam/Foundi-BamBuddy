/**
 * One eject flow, three doors.
 *
 * Every eject affordance on a printer card — the overflow item, the expanded
 * card's plate banner, the compact card's icon — calls the same `eject()`. The
 * backend answers with one of three shapes and this hook owns the routing:
 *
 *   200                → success toast, caches invalidated.
 *   409 `bed_hot`      → the hot-bed confirm (re-calls with allowHot=true,
 *                        carrying the profile / height / declaration through).
 *   409 `foreign_plate`→ the eject dialog: the operator checks the part height
 *                        and picks the sweep profile, then confirms.
 *   any other code     → an i18n'd toast. An unrecognized plate NEVER dead-ends
 *                        in a toast; that is what the dialog is for.
 *
 * The `declare_occupied` leg is the on-demand door: the server raises the plate
 * gate itself and continues into the foreign flow, so the 409 is expected. The
 * raise is NOT rolled back — not by the 409, not by the operator cancelling the
 * dialog. The plate IS occupied either way; "Mark plate as cleared" is the undo.
 */

import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api, ApiError } from '../api/client';
import type { PrinterStatus } from '../api/client';
import type { EjectProfile } from '../types/ejectProfiles';
import { useToast } from '../contexts/ToastContext';

/** Who put the part on the plate, per the backend's `foreign_plate` detail.
 *  Decides the dialog title only — the flow is identical for all three. */
export type EjectDialogOrigin = 'foreign' | 'farm_unit' | 'declared';

/** Open eject dialog. Null when closed. */
export interface EjectDialogState {
  origin: EjectDialogOrigin;
  /** Name of the print that deposited the part; null when unidentified. */
  printName: string | null;
  /** Height parsed from the donor 3MF; null when the backend could not read one
   *  (the operator must then supply it — the sweep clearance depends on it). */
  maxZHeightMm: number | null;
  suggestedEjectProfileId: number | null;
  /** Carried from the call that opened this dialog, so the confirm re-sends it. */
  declareOccupied: boolean;
}

/** Open hot-bed confirm. Null when closed. */
export interface EjectHotConfirmState {
  bedC: number;
  thresholdC: number;
  ejectProfileId: number | null;
  declareOccupied: boolean;
  maxZHeightMm: number | null;
}

/** Public argument of `eject()`; every field defaults to the first-click shape. */
export interface EjectOptions {
  allowHot?: boolean;
  ejectProfileId?: number | null;
  declareOccupied?: boolean;
  maxZHeightMm?: number | null;
}

/** Which leg issued a call — decides where its failure is rendered. A dialog
 *  confirm keeps the failure inside the dialog (a toast would vanish while the
 *  operator is reading the height they have to correct). */
type EjectLeg = 'door' | 'dialog' | 'hot';

interface EjectVars extends Required<EjectOptions> {
  leg: EjectLeg;
}

export interface UseEjectPlate {
  /** Start (or retry) an eject. No args = the first-click shape. */
  eject: (options?: EjectOptions) => void;
  dialog: EjectDialogState | null;
  /** Profiles for the dialog picker; only fetched while it is open. */
  ejectProfiles: EjectProfile[];
  /** Operator override → backend suggestion → first profile. */
  selectedProfileId: number | null;
  setSelectedProfileId: (id: number | null) => void;
  /** Raw input string so the field can be cleared and retyped. */
  heightInput: string;
  setHeightInput: (value: string) => void;
  /** The confirm gate: a blank or non-positive height must not reach the
   *  backend — `max_z` sets the sweep clearance and lift. */
  heightValid: boolean;
  /** Confirm-leg failure text, rendered inside the open dialog. */
  dialogError: string | null;
  confirmDialog: () => void;
  closeDialog: () => void;
  hotConfirm: EjectHotConfirmState | null;
  confirmHot: () => void;
  closeHotConfirm: () => void;
  isPending: boolean;
}

/** `origin` is absent on a backend predating the field; a plate the farm cannot
 *  attribute is foreign by definition, so that is the safe read. */
function parseOrigin(value: unknown): EjectDialogOrigin {
  return value === 'farm_unit' || value === 'declared' ? value : 'foreign';
}

/** A height the backend could not determine reads as null, not 0 — 0 would
 *  prefill a value the operator might confirm unread. */
function parseHeight(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

/** Prefill at the field's own precision (step 0.1) so it never opens on a float
 *  artifact the operator would have to retype. */
function formatHeight(mm: number | null): string {
  return mm === null ? '' : String(Math.round(mm * 10) / 10);
}

/** A temperature the backend could not read. `Number(null)` is a finite 0, so
 *  the absence has to be tested before the conversion — otherwise a confirm
 *  offers to sweep a "0 °C" bed nobody measured. */
function parseTemp(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function useEjectPlate(printerId: number): UseEjectPlate {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [dialog, setDialog] = useState<EjectDialogState | null>(null);
  const [profileOverride, setProfileOverride] = useState<number | null>(null);
  const [heightInput, setHeightInputRaw] = useState('');
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [hotConfirm, setHotConfirm] = useState<EjectHotConfirmState | null>(null);

  // Shared cache entry with the eject-profiles page, the SKU form and the run
  // form — one key fleet-wide.
  const { data: ejectProfilesData } = useQuery({
    queryKey: ['eject-profiles'],
    queryFn: api.getEjectProfiles,
    enabled: dialog !== null,
  });
  const ejectProfiles = ejectProfilesData ?? [];

  // Derived (not synced), so no effect is needed when the profiles land.
  const selectedProfileId =
    profileOverride ?? dialog?.suggestedEjectProfileId ?? ejectProfiles[0]?.id ?? null;

  const heightMm = Number(heightInput);
  const heightValid = heightInput.trim() !== '' && Number.isFinite(heightMm) && heightMm > 0;

  const closeDialog = () => {
    setDialog(null);
    setProfileOverride(null);
    setDialogError(null);
  };

  /** The server RAISED the gate before it answered, and does not roll that back
   *  — reflect it so the card's gate affordances stand behind the dialog (they
   *  are the operator's undo if this is cancelled). ['printers'] is invalidated
   *  by hand because the fleet badge normally refetches off a `printer_status`
   *  frame this lane does not produce. */
  const reflectDeclaredGate = () => {
    queryClient.setQueryData(['printerStatus', printerId], (old: PrinterStatus | undefined) =>
      old ? { ...old, awaiting_plate_clear: true } : old,
    );
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  /** One i18n'd sentence per refusal code. Returns null for a code this build
   *  does not know, so the caller falls back to the generic failure. */
  const messageForCode = (error: ApiError): string | null => {
    const detail = error.detail ?? {};
    switch (error.code) {
      // `printer_busy` is the pre-authority spelling of the same refusal.
      case 'job_active':
      case 'printer_busy':
        return t('printers.eject.error.jobActive');
      case 'dispatch_in_flight':
        return t('printers.eject.error.dispatchInFlight');
      case 'eject_in_flight': {
        const started = detail.started === true;
        // `age_s` is nullable — Number(null) is a finite 0, so the type has to
        // be the test, not Number.isFinite.
        const age = typeof detail.age_s === 'number' ? Math.round(detail.age_s) : null;
        if (age === null) {
          return started
            ? t('printers.eject.error.ejectInFlightStartedNoAge')
            : t('printers.eject.error.ejectInFlightPendingNoAge');
        }
        return started
          ? t('printers.eject.error.ejectInFlightStarted', { age })
          : t('printers.eject.error.ejectInFlightPending', { age });
      }
      case 'bed_unreadable':
        return t('printers.eject.error.bedUnreadable');
      // `no_eligible_unit` is the pre-authority spelling of "no donor file".
      case 'no_donor':
      case 'no_eligible_unit':
        return t('printers.eject.error.noDonor');
      case 'first_article':
        return t('printers.eject.error.firstArticle');
      case 'not_connected':
        return t('printers.eject.error.notConnected');
      case 'eject_dispatch_failed':
        return t('printers.eject.error.dispatchFailed', {
          message: typeof detail.message === 'string' ? detail.message : error.message,
        });
      case 'profile_not_found':
        return t('printers.eject.error.profileNotFound');
      case 'no_plate_gate':
        return t('printers.eject.error.noPlateGate');
      default:
        return null;
    }
  };

  /** The one sentence a failure is shown as, wherever it is rendered. A
   *  structured code this build has no key for falls back to the generic
   *  failure rather than leaking backend English (or a bare "HTTP 409"); a
   *  plain-string detail IS the useful sentence — it carries the eject
   *  generator's own guard text — so it survives verbatim. */
  const resolveMessage = (error: Error): string => {
    if (error instanceof ApiError) {
      const mapped = messageForCode(error);
      if (mapped) return mapped;
      if (error.code) return t('printers.toast.failedToSendCommand');
    }
    return error.message || t('printers.toast.failedToSendCommand');
  };

  const ejectMutation = useMutation({
    mutationFn: (vars: EjectVars) =>
      api.ejectNow(
        printerId,
        vars.allowHot,
        vars.ejectProfileId,
        vars.declareOccupied,
        vars.maxZHeightMm,
      ),
    onSuccess: () => {
      closeDialog();
      setHotConfirm(null);
      showToast(t('printers.eject.dispatched'));
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
      queryClient.invalidateQueries({ queryKey: ['queue', printerId] });
    },
    onError: (error: Error, vars) => {
      if (error instanceof ApiError && error.detail) {
        // A hot bed on the dialog's confirm closes that dialog and opens the
        // hot-bed confirm, which carries the operator's profile and height back
        // into the re-call. Never on the already-confirmed hot leg.
        if (error.code === 'bed_hot' && !vars.allowHot) {
          const bedC = parseTemp(error.detail.bed_c);
          const thresholdC = parseTemp(error.detail.threshold_c);
          if (bedC !== null && thresholdC !== null) {
            closeDialog();
            setHotConfirm({
              bedC,
              thresholdC,
              ejectProfileId: vars.ejectProfileId,
              declareOccupied: vars.declareOccupied,
              maxZHeightMm: vars.maxZHeightMm,
            });
            return;
          }
        }
        if (error.code === 'foreign_plate') {
          const maxZHeightMm = parseHeight(error.detail.max_z_height_mm);
          const suggested = error.detail.suggested_eject_profile_id;
          setProfileOverride(null);
          setDialogError(null);
          setHeightInputRaw(formatHeight(maxZHeightMm));
          setDialog({
            origin: parseOrigin(error.detail.origin),
            printName:
              typeof error.detail.print_name === 'string' ? error.detail.print_name : null,
            maxZHeightMm,
            suggestedEjectProfileId: typeof suggested === 'number' ? suggested : null,
            declareOccupied: vars.declareOccupied,
          });
          if (vars.declareOccupied) reflectDeclaredGate();
          return;
        }
      }
      const message = resolveMessage(error);
      // The dialog stays open on its own confirm's failures, so they belong
      // inside it (the operator corrects the height or profile and retries).
      if (vars.leg === 'dialog') {
        setDialogError(message);
        return;
      }
      setHotConfirm(null);
      showToast(message, 'error');
    },
  });

  const run = (leg: EjectLeg, options: EjectOptions) =>
    ejectMutation.mutate({
      leg,
      allowHot: options.allowHot ?? false,
      ejectProfileId: options.ejectProfileId ?? null,
      declareOccupied: options.declareOccupied ?? false,
      maxZHeightMm: options.maxZHeightMm ?? null,
    });

  return {
    eject: (options: EjectOptions = {}) => run('door', options),
    dialog,
    ejectProfiles,
    selectedProfileId,
    setSelectedProfileId: (id: number | null) => {
      setProfileOverride(id);
      setDialogError(null);
    },
    heightInput,
    setHeightInput: (value: string) => {
      setHeightInputRaw(value);
      setDialogError(null);
    },
    heightValid,
    dialogError,
    confirmDialog: () => {
      if (!dialog || selectedProfileId === null || !heightValid) return;
      run('dialog', {
        allowHot: false,
        ejectProfileId: selectedProfileId,
        declareOccupied: dialog.declareOccupied,
        maxZHeightMm: heightMm,
      });
    },
    closeDialog,
    hotConfirm,
    confirmHot: () => {
      if (!hotConfirm) return;
      run('hot', {
        allowHot: true,
        ejectProfileId: hotConfirm.ejectProfileId,
        declareOccupied: hotConfirm.declareOccupied,
        maxZHeightMm: hotConfirm.maxZHeightMm,
      });
    },
    closeHotConfirm: () => setHotConfirm(null),
    isPending: ejectMutation.isPending,
  };
}
