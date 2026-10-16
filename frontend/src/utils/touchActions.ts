/**
 * The touch tile's contextual primary buttons: which verbs the printer's state asks for
 * right now, most pressing first, at most two (More carries the rest). Pure; the tile
 * renders the ids and wires them to `usePrinterActions`.
 *
 * Read from the backend's verdicts only: the plate-check human turn is
 * `status.plate_check_exit`, the Recover reach is `utils/printerRecovery`, the hold is
 * the maintenance hold. Nothing here re-derives a hold.
 */
import type { Printer, PrinterStatus } from '../api/client';
import { inFlightEject, recoverEffects } from './printerRecovery';

export type TilePrimaryAction =
  | 'ignoreResume'
  | 'stop'
  | 'exitMaintenance'
  | 'resume'
  | 'recover'
  | 'markCleared'
  | 'camera';

export const TILE_PRIMARY_MAX = 2;

/**
 * Recover as a PRIMARY button: the printer is quarantined, held by a fault whose exit
 * is Recover, or its eject has outlived its watchdog. A bare plate gate or dispatch
 * lease leaves Recover to More — Mark plate cleared is the verb for a plate.
 */
function recoverIsPrimary(printer: Printer, status: PrinterStatus | undefined): boolean {
  const effects = recoverEffects(printer, status);
  return (
    effects.includes('quarantine') ||
    effects.includes('equipment_fault') ||
    inFlightEject(status)?.runtime_exceeded === true
  );
}

/** The contextual primary buttons for this printer, in order, capped at `max`. */
export function tilePrimaryActions(
  printer: Printer,
  status: PrinterStatus | undefined,
  max: number = TILE_PRIMARY_MAX,
): TilePrimaryAction[] {
  const state = status?.state ?? null;
  const isPrinting = state === 'RUNNING' || state === 'PAUSE';
  const held = (printer.service_hold ?? status?.service_hold ?? null) !== null;
  const out: TilePrimaryAction[] = [];
  const add = (action: TilePrimaryAction) => {
    if (!out.includes(action)) out.push(action);
  };

  if (status?.plate_check_exit) {
    add('ignoreResume');
    add('stop');
  }
  if (held) add('exitMaintenance');
  if (state === 'PAUSE') add('resume');
  if (recoverIsPrimary(printer, status)) add('recover');
  // A gate under an armed cooldown watch is the farm's own loop (the eject comes); a
  // clear there would cancel it, so the button waits for a gate a person must clear.
  if (status?.awaiting_plate_clear === true && !isPrinting && !status.eject_watch) add('markCleared');
  if (state === 'RUNNING') add('camera');
  return out.slice(0, Math.max(0, max));
}
