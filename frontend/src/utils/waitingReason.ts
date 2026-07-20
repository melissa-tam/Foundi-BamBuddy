/**
 * Waiting-reason copy: machine codes → i18n. Anything not a known machine code
 * is either a token we humanize on the fly (snake_case → sentence case) or an
 * already-human backend sentence (e.g. a capability-gate reason) that is passed
 * through verbatim.
 *
 * Single implementation shared by the run detail page, the printer-card farm
 * chip, the queue rows and the pause-reason chip — the reason vocabulary lives
 * here so no view re-maps it. The keys reuse the existing
 * `productionRuns.detail.waiting.*` bundle.
 */

/** A machine token: lowercase words, digits and underscores only (e.g.
 *  `stagger_hold`). Backend-authored sentences (spaces, punctuation, capitals)
 *  are NOT token-shaped and are shown as-is. */
const TOKEN_SHAPE = /^[a-z0-9_]+$/;

/** Whether `reason` is a bare machine token vs an already-human sentence. */
export function isTokenShaped(reason: string): boolean {
  return TOKEN_SHAPE.test(reason);
}

/**
 * Humanize a snake_case machine token into operator-plain copy: underscores →
 * spaces, first letter capitalized. `stagger_hold` → "Stagger hold". This is the
 * no-i18n-key fallback for tokens the map below does not cover; callers pass an
 * already-human sentence through unchanged instead.
 */
export function humanizeToken(token: string): string {
  const spaced = token.replace(/_/g, ' ').trim();
  if (!spaced) return token;
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function waitingReasonText(reason: string | null, t: (k: string) => string): string | null {
  if (!reason) return null;
  switch (reason) {
    case 'printer_offline_stalled':
      return t('productionRuns.detail.waiting.printerOfflineStalled');
    case 'print_paused_stalled':
      return t('productionRuns.detail.waiting.printPausedStalled');
    case 'plate_not_empty_printer_detected':
      return t('productionRuns.detail.waiting.visionHold');
    case 'previous_print_failed':
      return t('productionRuns.detail.waiting.previousPrintFailed');
    case 'filament_short':
      return t('productionRuns.detail.waiting.filamentShort');
    case 'start_spool_below_minimum':
      return t('productionRuns.detail.waiting.startSpoolBelowMinimum');
    case 'no_usb_drive':
      return t('productionRuns.detail.waiting.no_usb_drive');
    case 'stagger_hold':
      return t('productionRuns.detail.waiting.staggerHold');
    case 'spool_jam_recovering':
      return t('productionRuns.detail.waiting.spoolJamRecovering');
    case 'spool_jam_recovery_failed':
      return t('productionRuns.detail.waiting.spoolJamRecoveryFailed');
    case 'filament_runout_recovery_failed':
      return t('productionRuns.detail.waiting.filamentRunoutRecoveryFailed');
    default:
      // An unmapped token gets humanized (never shown raw to an operator); a
      // backend-authored sentence is already readable and passes through.
      return isTokenShaped(reason) ? humanizeToken(reason) : reason;
  }
}
