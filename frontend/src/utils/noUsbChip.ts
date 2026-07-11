// Fail-safe "No USB drive" chip gate for the printer card (#F8).
//
// The H2S has no microSD slot, so a required USB-A drive is the only storage
// media the printer can report. No drive means FTPS 553 on every dispatch, so
// we pre-warn the operator BEFORE a run stalls. The chip is deliberately
// fail-SAFE: it renders ONLY on an explicit reported `sdcard === false`, never
// on missing/undefined data. `status.state` must be truthy too — the backend
// PrinterState defaults `sdcard` to False before the first full status report
// arrives, and a truthy gcode_state proves a real report has landed.
import type { PrinterStatus } from '../api/client';

/** Minimal shape needed to decide the chip — keeps the helper unit-testable. */
export type NoUsbChipInput = Pick<PrinterStatus, 'connected' | 'state' | 'sdcard'>;

/**
 * True when the printer is connected, has emitted a real status report, and
 * explicitly reports no storage drive present.
 */
export function showNoUsbChip(status: NoUsbChipInput | null | undefined): boolean {
  if (!status) return false;
  return status.connected === true && !!status.state && status.sdcard === false;
}
