/**
 * The UI status buckets a printer is shown under (Problem / Printing / Paused /
 * Finished / Idle / Offline): the Printers page's grouping and bulk selection, and the
 * touch tile's state words. One classifier, one label per bucket.
 */
import type { HMSError } from '../api/client';
import type { PrinterState } from '../components/BulkPrinterToolbar';
import { hmsTone } from './hmsTone';

export const STATUS_GROUP_ORDER: string[] = ['error', 'printing', 'paused', 'finished', 'idle', 'offline'];

export const STATUS_GROUP_META: Record<string, { labelKey: string; dot: string }> = {
  error:    { labelKey: 'printers.status.problem',   dot: 'bg-status-error' },
  printing: { labelKey: 'printers.status.printing',  dot: 'bg-bambu-green animate-pulse' },
  paused:   { labelKey: 'printers.status.paused',    dot: 'bg-status-warning' },
  finished: { labelKey: 'printers.status.finished',  dot: 'bg-blue-400' },
  idle:     { labelKey: 'printers.status.idle',       dot: 'bg-bambu-green' },
  offline:  { labelKey: 'printers.status.offline',   dot: 'bg-gray-400' },
};

/** Classify a printer into one of the UI status buckets. */
export function classifyPrinterStatus(
  status: { connected: boolean; state: string | null; hms_errors?: HMSError[] } | undefined,
): PrinterState {
  if (!status?.connected) return 'offline';
  // Error bucket = error-level HMS tone (fatal/serious code, or a FAILED print
  // carrying any error). Unknown codes are no longer filtered out (H1).
  if (hmsTone(status.hms_errors, status.state) === 'error') return 'error';
  switch (status.state) {
    case 'RUNNING': return 'printing';
    case 'PAUSE':   return 'paused';
    case 'FINISH':  return 'finished';
    // FAILED without an error-level fault is the printer's terminal state after
    // any unsuccessful end — including user-cancellations. Treat the same as
    // FINISH for grouping/badging purposes; the error escalation is the
    // early-return above.
    case 'FAILED':  return 'finished';
    default:        return 'idle';
  }
}
