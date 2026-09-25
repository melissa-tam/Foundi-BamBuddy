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

/**
 * The CLOSED set of eject refusal codes.
 *
 * The TypeScript mirror of `EjectRefusalReason` in
 * `backend/app/schemas/printer.py`, which OWNS this vocabulary — there is no
 * generated client, so the two are kept spelling-identical by hand and by the
 * test that walks this list looking for a sentence.
 *
 * Why a list and not just a union: `ejectRefusalCopy` switches over it
 * exhaustively with a `never` default, so a code added to the backend and
 * mirrored here without copy is a COMPILE error rather than a generic "failed
 * to send command" toast. That is the failure this list exists to end —
 * `z_unreferenced` shipped on the backend and reached operators as the generic
 * toast, on the one refusal that needs a human to walk over and clear a plate.
 */
export const EJECT_REFUSAL_CODES = [
  'job_active',
  'dispatch_in_flight',
  'eject_in_flight',
  'z_unreferenced',
  'not_connected',
  'no_plate_gate',
  'bed_unreadable',
  'first_article',
  'no_donor',
  'not_found',
  'profile_not_found',
] as const;

export type EjectRefusalCode = (typeof EJECT_REFUSAL_CODES)[number];

/**
 * Codes the eject lane can send that are NOT in the closed refusal union: two
 * pre-authority spellings kept alive for an older server, and the dispatch
 * failure, which is raised by the dispatcher's own exception rather than by an
 * occupancy verdict.
 */
export const EJECT_EXTRA_CODES = [
  'printer_busy',
  'no_eligible_unit',
  'eject_dispatch_failed',
] as const;

const REFUSAL_CODE_SET: ReadonlySet<string> = new Set(EJECT_REFUSAL_CODES);

/** One refusal's copy: the leaf to render, and whatever it interpolates. */
export interface EjectRefusalCopy {
  key: string;
  params?: Record<string, string | number>;
}

/**
 * Code to copy. Pure, so a test can walk every code the backend can send and
 * prove each one reaches a leaf that exists.
 *
 * Returns null ONLY for a code this build has never heard of — the caller then
 * falls back to the generic failure. A code inside the mirrored union can never
 * return null, because the switch below cannot compile with a member missing.
 */
export function ejectRefusalCopy(
  code: string,
  detail: Record<string, unknown>,
  fallbackMessage: string,
): EjectRefusalCopy | null {
  // The three outside the closed union first: two are older spellings of a
  // token that IS in it, and folding them here keeps the switch below a
  // faithful mirror rather than a mirror plus history.
  switch (code) {
    case 'printer_busy':
      return { key: 'printers.eject.error.jobActive' };
    case 'no_eligible_unit':
      return { key: 'printers.eject.error.noDonor' };
    case 'eject_dispatch_failed':
      return {
        key: 'printers.eject.error.dispatchFailed',
        params: {
          message: typeof detail.message === 'string' ? detail.message : fallbackMessage,
        },
      };
    default:
      break;
  }

  if (!REFUSAL_CODE_SET.has(code)) return null;

  switch (code as EjectRefusalCode) {
    case 'job_active':
      return { key: 'printers.eject.error.jobActive' };
    case 'dispatch_in_flight':
      return { key: 'printers.eject.error.dispatchInFlight' };
    case 'eject_in_flight': {
      const started = detail.started === true;
      // `age_s` is nullable — Number(null) is a finite 0, so the type has to be
      // the test, not Number.isFinite.
      const age = typeof detail.age_s === 'number' ? Math.round(detail.age_s) : null;
      if (age === null) {
        return {
          key: started
            ? 'printers.eject.error.ejectInFlightStartedNoAge'
            : 'printers.eject.error.ejectInFlightPendingNoAge',
        };
      }
      return {
        key: started
          ? 'printers.eject.error.ejectInFlightStarted'
          : 'printers.eject.error.ejectInFlightPending',
        params: { age },
      };
    }
    case 'z_unreferenced':
      return { key: 'printers.eject.error.zUnreferenced' };
    case 'not_connected':
      return { key: 'printers.eject.error.notConnected' };
    case 'no_plate_gate':
      return { key: 'printers.eject.error.noPlateGate' };
    case 'bed_unreadable':
      return { key: 'printers.eject.error.bedUnreadable' };
    case 'first_article':
      return { key: 'printers.eject.error.firstArticle' };
    case 'no_donor':
      return { key: 'printers.eject.error.noDonor' };
    case 'not_found':
      return { key: 'printers.eject.error.printerNotFound' };
    case 'profile_not_found':
      return { key: 'printers.eject.error.profileNotFound' };
    default: {
      // The compiler's own check: a mirrored code with no case above leaves
      // `code` as something other than `never` here, and this line stops
      // building. That is the whole point of the list above being closed.
      const unhandled: never = code as never;
      return unhandled;
    }
  }
}

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
  /** The limit the bed missed; null when the server had neither an eject line
   *  (shop air unknown) nor a chamber reading to judge the bed by. */
  thresholdC: number | null;
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
    if (error.code === null) return null;
    const copy = ejectRefusalCopy(error.code, error.detail ?? {}, error.message);
    return copy === null ? null : t(copy.key, copy.params);
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
          // The bed reading is what the confirm is about; a missing limit is
          // its own confirm body, never a reason to drop the confirm.
          const bedC = parseTemp(error.detail.bed_c);
          const thresholdC = parseTemp(error.detail.threshold_c);
          if (bedC !== null) {
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
