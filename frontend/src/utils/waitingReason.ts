/**
 * Waiting-reason copy: machine codes → i18n. Anything not a known machine code
 * is already a human-readable backend sentence (e.g. a capability-gate reason)
 * and is passed through verbatim.
 *
 * Single implementation shared by the run detail page and the printer-card farm
 * chip — the reason vocabulary lives here so neither view re-maps it. The keys
 * reuse the existing `productionRuns.detail.waiting.*` bundle.
 */
export function waitingReasonText(reason: string | null, t: (k: string) => string): string | null {
  if (!reason) return null;
  switch (reason) {
    case 'printer_offline_stalled':
      return t('productionRuns.detail.waiting.printerOfflineStalled');
    case 'plate_not_empty_printer_detected':
      return t('productionRuns.detail.waiting.visionHold');
    case 'previous_print_failed':
      return t('productionRuns.detail.waiting.previousPrintFailed');
    case 'filament_short':
      return t('productionRuns.detail.waiting.filamentShort');
    default:
      return reason;
  }
}
