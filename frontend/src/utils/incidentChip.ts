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

/** The toolhead variant's tooltip: what the farm is doing about the empty toolhead. */
function toolheadTooltip(refill: ToolheadRefillState | null): ChipCopy[] {
  if (refill === null) return [TOOLHEAD_EXIT];
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

function toolheadChip(refill: ToolheadRefillState | null): HoldChip {
  return {
    variant: 'toolhead',
    tone: refill?.phase === 'loading' ? 'acting' : 'held',
    label: TOOLHEAD_EMPTY_LABEL,
    tooltip: toolheadTooltip(refill),
    qualifier: null,
  };
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
 *    red: a person loads a slot (or a Bambuddy Resume refills it first).
 */
export function holdChip(status: HoldChipStatus): HoldChip | null {
  const refill = status.toolhead?.refill ?? null;
  if (refill !== null) return toolheadChip(refill);

  const incident = status.open_incident ?? null;
  if (incident !== null && !OWN_SURFACE_INCIDENT_KINDS.includes(incident.kind)) {
    return incidentChip(incident);
  }

  if (status.state === 'PAUSE' && status.toolhead?.feed === 'empty') return toolheadChip(null);
  return null;
}
