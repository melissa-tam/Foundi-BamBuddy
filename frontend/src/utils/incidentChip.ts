// The printer-card hold chip: WHICH hold it names, in which tone, and the tooltip
// sentences it carries — decided once here, rendered by `components/HoldChip`.
// Labels and tooltips are looked up per kind (`printers.incident.<kind>`,
// `printers.incidentAction.<kind>`) — the compiler cannot see these dynamic keys,
// so `__tests__/i18n/incidentKinds.test.ts` pins them.
import { OWN_SURFACE_INCIDENT_KINDS } from '../api/client';
import type {
  OpenIncidentState,
  PrinterIncidentKind,
  PrinterStatus,
  ToolheadRefillAnswer,
  ToolheadRefillCommand,
  ToolheadRefillReason,
  ToolheadRefillState,
} from '../api/client';
import { incidentKindLabelKey } from './fleetMetrics';

/**
 * Kinds whose `incidentAction` names a PERSON's exits that do not exist while
 * the farm still acts, so the chip shows `printers.incidentRecoveringAction.<kind>`
 * until the row is `escalated`.
 *
 * `plate_vision`: the farm re-checks and stop-and-retries first; "Ignore and
 * resume" appears only on the person's turn (`PrinterStatus.plate_check_exit`),
 * and the backend answers an earlier press with HTTP 409.
 *
 * `toolhead_refill`: the farm refills the toolhead and resumes on its own; "Load a
 * slot, then resume" is a person's act only once the refill has handed over.
 *
 * Every other kind keeps its one instruction in both states: a `runout` row is
 * `recovering` while it waits for the operator's refill, so its instruction is
 * what the operator needs.
 */
export const RECOVERING_ACTION_KINDS: readonly PrinterIncidentKind[] = ['plate_vision', 'toolhead_refill'];

/** The i18n key of the chip's tooltip for this hold, in its current state. */
export function incidentActionKey(incident: Pick<OpenIncidentState, 'kind' | 'status'>): string {
  return incident.status !== 'escalated' && RECOVERING_ACTION_KINDS.includes(incident.kind)
    ? `printers.incidentRecoveringAction.${incident.kind}`
    : `printers.incidentAction.${incident.kind}`;
}

/** `acting` — the farm is still acting (amber); `held` — it is a person's turn (red). */
export type HoldChipTone = 'acting' | 'held';

/** One translatable sentence: an i18n key and its interpolation values. */
export interface ChipCopy {
  key: string;
  values?: Record<string, string>;
}

/**
 * The chip as the card renders it. Two variants of ONE chip:
 *   - `incident` — names the open `printer_incident` row the backend ranked first;
 *   - `toolhead` — names the EMPTY toolhead: the farm loading it, a farm load that
 *     did not reach it, or a paused print sitting on it with nothing open.
 */
export interface HoldChip {
  variant: 'incident' | 'toolhead';
  tone: HoldChipTone;
  /** The pill's noun. */
  label: ChipCopy;
  /** The tooltip's sentences, in reading order (supplementary detail: the tooltip, never inline). */
  tooltip: readonly ChipCopy[];
  /** A raw qualifier the tooltip ends with (the hold's slot), or null. */
  qualifier: string | null;
}

/** The status fields the chip reads. */
export type HoldChipStatus = Pick<PrinterStatus, 'state' | 'open_incident' | 'toolhead'>;

/** "Toolhead empty" — one leaf for the kind and the toolhead variant: the same fact. */
const TOOLHEAD_EMPTY_LABEL: ChipCopy = { key: incidentKindLabelKey('toolhead_refill') };
/** The person's act on an empty toolhead — the kind's own instruction. */
const TOOLHEAD_EXIT: ChipCopy = { key: 'printers.incidentAction.toolhead_refill' };

/**
 * A failed refill's sentences per failed STEP: the lead (with / without the slot) and
 * what the AMS answered. `acted` names the step ("…the unload did not finish"), so an
 * unload that stopped part-way never reads as a load; `no_movement` is the same fact
 * for both. A `Record`, so a new step without copy fails `tsc -b`.
 */
const FAILED_COPY: Record<
  ToolheadRefillCommand,
  { slot: string; anySlot: string; answer: Record<ToolheadRefillAnswer, string> }
> = {
  load: {
    slot: 'printers.toolhead.loadFailed',
    anySlot: 'printers.toolhead.loadFailedAnySlot',
    answer: { no_movement: 'printers.toolhead.answer.no_movement', acted: 'printers.toolhead.answer.loadActed' },
  },
  unload: {
    slot: 'printers.toolhead.unloadFailed',
    anySlot: 'printers.toolhead.unloadFailedAnySlot',
    answer: { no_movement: 'printers.toolhead.answer.no_movement', acted: 'printers.toolhead.answer.unloadActed' },
  },
};

/** The refill variant's tooltip: the farm's load in flight, or the step that failed. */
function refillTooltip(refill: ToolheadRefillState): ChipCopy[] {
  if (refill.phase === 'loading') {
    return [
      refill.slot
        ? { key: 'printers.toolhead.loading', values: { slot: refill.slot } }
        : { key: 'printers.toolhead.loadingAnySlot' },
    ];
  }
  // A backend predating `command` only ever reported a failed load.
  const copy = FAILED_COPY[refill.command ?? 'load'];
  return [
    refill.slot ? { key: copy.slot, values: { slot: refill.slot } } : { key: copy.anySlot },
    ...(refill.answer ? [{ key: copy.answer[refill.answer] }] : []),
    TOOLHEAD_EXIT,
  ];
}

/**
 * Rule (3)'s tooltip per the backend's T3 verdict (`toolhead.refill_reason`: what a
 * Bambuddy Resume would do about the empty toolhead NOW — served, never re-derived here),
 * or `null`: the chip says nothing. Only four verdicts are a hold a person should see:
 * `owed` (the Resume loads first), `maintenance` and `physical` (the farm will not load —
 * a person does) and `command_pending` (the farm loads when the AMS runs its queued
 * command). Every other verdict is the printer's own business or no hold at all —
 * `before_first_layer` (the start block loads; 012-H2S 2026-10-10, PAUSEd at the
 * plate-marker dialog with `tray_now` 255), `runout_demand` (the firmware asks for the
 * same slot), `power_loss_prompt`, `last_layer`, `change_in_flight`, `eject_sweep` — or
 * says nothing is wrong (`fed`) or nothing is known (`unknown`). A `Record` over EVERY
 * verdict, so a new one fails `tsc -b` until it is given copy or silence here.
 */
const PAUSED_EMPTY_TOOLTIP: Record<ToolheadRefillReason, string | null> = {
  owed: 'printers.toolhead.reason.owed',
  maintenance: 'printers.toolhead.reason.maintenance',
  physical: 'printers.toolhead.reason.physical',
  command_pending: 'printers.toolhead.reason.command_pending',
  fed: null,
  runout_demand: null,
  power_loss_prompt: null,
  before_first_layer: null,
  last_layer: null,
  change_in_flight: null,
  eject_sweep: null,
  unknown: null,
};

/** Rule (3)'s tooltip key, or null — for no verdict (null / absent) and a verdict this build does not know. */
function pausedEmptyTooltipKey(reason: ToolheadRefillReason | null | undefined): string | null {
  if (reason === null || reason === undefined) return null;
  return Object.prototype.hasOwnProperty.call(PAUSED_EMPTY_TOOLTIP, reason) ? PAUSED_EMPTY_TOOLTIP[reason] : null;
}

function toolheadChip(tone: HoldChipTone, tooltip: readonly ChipCopy[]): HoldChip {
  return { variant: 'toolhead', tone, label: TOOLHEAD_EMPTY_LABEL, tooltip, qualifier: null };
}

function incidentChip(incident: OpenIncidentState): HoldChip {
  const escalated = incident.status === 'escalated';
  return {
    variant: 'incident',
    tone: escalated ? 'held' : 'acting',
    label: { key: escalated ? incidentKindLabelKey(incident.kind) : 'printers.incident.recovering' },
    tooltip: [{ key: incidentActionKey(incident) }],
    qualifier: incident.slot_desc,
  };
}

/**
 * The hold chip this printer shows, or null. ONE order, first match wins:
 *
 * 1. **The farm's refill state** (`toolhead.refill`, set only while the toolhead reads
 *    empty or unknown) → the toolhead variant: amber "loading", red "failed". It
 *    outranks every incident row, because it rides one — an AMS row the refill
 *    re-entered, or its own `toolhead_refill` row — and it is the newer, more exact
 *    fact: a failed load under a jam row is what keeps the resume from going on, and
 *    the slot it names is the operator's next act.
 * 2. **The open incident row** (`open_incident`, the backend's top-ranked by
 *    `KIND_PRECEDENCE`; kinds with their own surface skipped) → the incident variant.
 *    A jam / runout / physical row names the fault the farm is answering; a plate
 *    check, a power-loss prompt or a lost Z reference is a pause the farm does NOT
 *    refill (the job before its first layer, the firmware's own prompt), so a bare
 *    empty toolhead under them must not take the chip. A `toolhead_refill` row reads
 *    "Toolhead empty" once escalated and "Recovering" while the farm still acts.
 * 3. **A paused print on an empty toolhead** with nothing open → the toolhead variant,
 *    red, ONLY when the backend's verdict (`refill_reason`) is one a person should see
 *    (`PAUSED_EMPTY_TOOLTIP`); its tooltip says what a Resume would do. An empty toolhead
 *    the printer fills itself (before the first layer, a runout demand, …), a null verdict
 *    or an older backend with no verdict shows no chip.
 */
export function holdChip(status: HoldChipStatus): HoldChip | null {
  const refill = status.toolhead?.refill ?? null;
  if (refill !== null) return toolheadChip(refill.phase === 'loading' ? 'acting' : 'held', refillTooltip(refill));

  const incident = status.open_incident ?? null;
  if (incident !== null && !OWN_SURFACE_INCIDENT_KINDS.includes(incident.kind)) {
    return incidentChip(incident);
  }

  if (status.state === 'PAUSE' && status.toolhead?.feed === 'empty') {
    const key = pausedEmptyTooltipKey(status.toolhead.refill_reason);
    if (key !== null) return toolheadChip('held', [{ key }]);
  }
  return null;
}
