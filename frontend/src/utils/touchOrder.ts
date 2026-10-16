/**
 * THE owner of the touch view's tile order and of which tiles carry a camera still.
 *
 * Printers needing attention lead, in a fixed rank (`ATTENTION_ORDER`), then every
 * printer by name. The rank reads the backend's verdicts as they arrive — the hold
 * chip's tone (`PrinterStatus.hold_chip`, never re-derived here), the plate gate, the
 * cooldown watch, the maintenance hold — and decides nothing about a hold itself.
 *
 * While the screen is being touched the order must not move under a finger, so the
 * page keeps the order it showed (`holdOrder`) and re-sorts once the screen has been
 * idle; this module stays pure and holds no clock.
 */
import type { Printer, PrinterStatus } from '../api/client';
import { hmsTone } from './hmsTone';

/** Why a tile leads the touch view, most urgent first. */
export const ATTENTION_ORDER = [
  'held',
  'acting',
  'plateGated',
  'paused',
  'error',
  'maintenance',
  'cooldown',
] as const;

export type AttentionReason = (typeof ATTENTION_ORDER)[number];

/** One printer as the touch view reads it: the fleet row and its live status. */
export interface TouchEntry {
  printer: Printer;
  status: PrinterStatus | undefined;
}

/** The maintenance hold: the fleet row first, the status frame as the fallback. */
export function maintenanceHoldOf(entry: TouchEntry) {
  return entry.printer.service_hold ?? entry.status?.service_hold ?? null;
}

/** One predicate per reason — a `Record`, so a new reason cannot be left without one. */
const REASON_PRESENT: Record<AttentionReason, (entry: TouchEntry) => boolean> = {
  held: ({ status }) => status?.hold_chip?.tone === 'held',
  acting: ({ status }) => status?.hold_chip?.tone === 'acting',
  // A gate with an armed cooldown watch is the farm's own loop, not a person's plate.
  plateGated: ({ status }) => status?.awaiting_plate_clear === true && !status.eject_watch,
  paused: ({ status }) => status?.state === 'PAUSE',
  error: ({ printer, status }) =>
    printer.quarantined === true ||
    (status?.connected === true && hmsTone(status.hms_errors, status.state) === 'error'),
  maintenance: (entry) => maintenanceHoldOf(entry) !== null,
  cooldown: ({ status }) => !!status?.eject_watch,
};

/** The most urgent reason this printer needs attention, or null when it needs none. */
export function attentionOf(entry: TouchEntry): AttentionReason | null {
  return ATTENTION_ORDER.find((reason) => REASON_PRESENT[reason](entry)) ?? null;
}

const nameCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

function rankOf(entry: TouchEntry): number {
  const reason = attentionOf(entry);
  return reason === null ? ATTENTION_ORDER.length : ATTENTION_ORDER.indexOf(reason);
}

/** Attention first (by rank), then name (numeric-aware), then id — a total order. */
export function compareForTouch(a: TouchEntry, b: TouchEntry): number {
  return (
    rankOf(a) - rankOf(b) ||
    nameCollator.compare(a.printer.name, b.printer.name) ||
    a.printer.id - b.printer.id
  );
}

/** The printer ids in touch order. */
export function touchOrder(entries: readonly TouchEntry[]): number[] {
  return [...entries].sort(compareForTouch).map((entry) => entry.printer.id);
}

/**
 * The order to show while the screen is held: the ids of `held` that still exist, in
 * the order they were shown, then any printer that appeared since, in `fresh` order.
 */
export function holdOrder(held: readonly number[], fresh: readonly number[]): number[] {
  const present = new Set(fresh);
  const kept = held.filter((id) => present.has(id));
  const keptSet = new Set(kept);
  return [...kept, ...fresh.filter((id) => !keptSet.has(id))];
}

/**
 * Whether the tile shows the printer's camera still: a connected printer in error,
 * with a plate that is not empty, cooling for an eject, or in maintenance. Every other
 * tile shows its print instead, so the still owner is asked about these printers only.
 */
export function stillWanted(entry: TouchEntry): boolean {
  const { status } = entry;
  if (status?.connected !== true) return false;
  const inError = status.hold_chip != null || hmsTone(status.hms_errors, status.state) === 'error';
  const plateNotEmpty = status.awaiting_plate_clear === true || status.occupancy?.plate.occupied === true;
  return inError || plateNotEmpty || !!status.eject_watch || maintenanceHoldOf(entry) !== null;
}
