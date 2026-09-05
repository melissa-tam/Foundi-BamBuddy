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
      // The printer's own vision check tripped TWICE in the window: the farm
      // stopped the print and raised a human-clear plate gate. Copy (not the
      // key) changed in the 2026-09-04 wave — the print is stopped, not paused,
      // so "resume on the printer" is no longer a thing an operator can do.
      return t('productionRuns.detail.waiting.visionHold');
    case 'power_loss_hold':
      // The printer is sitting at the firmware's OWN power-loss prompt and the
      // farm's resume was not accepted. The humanized fallback ("Power loss
      // hold") names the cause but hides the only action that clears it — the
      // operator resuming on the printer's touchscreen; nothing in this UI can.
      return t('productionRuns.detail.waiting.powerLossHold');
    case 'z_reference_lost':
      // The printer rebooted with a part still on the plate, so its Z datum is
      // fiction and no eject may run against it. "Z reference lost" would read
      // as a calibration warning; the operator instruction is the point — take
      // the part off BY HAND (never via Eject plate, whose absolute Z moves
      // would run against the destroyed frame — the 002-H2S bed-floor drive).
      return t('productionRuns.detail.waiting.zReferenceLost');
    case 'previous_print_failed':
      return t('productionRuns.detail.waiting.previousPrintFailed');
    case 'filament_short':
      return t('productionRuns.detail.waiting.filamentShort');
    case 'start_spool_below_minimum':
      return t('productionRuns.detail.waiting.startSpoolBelowMinimum');
    case 'filament_unread_pending':
      return t('productionRuns.detail.waiting.filamentUnreadPending');
    case 'no_usb_drive':
      return t('productionRuns.detail.waiting.no_usb_drive');
    case 'library_file_missing':
      // A dispatch PRECONDITION, like no_usb_drive: the item's source file is
      // gone from disk, so nothing can be uploaded. The humanized fallback
      // ("Library file missing") would not tell the operator that restoring or
      // re-uploading the file is what releases the hold.
      return t('productionRuns.detail.waiting.libraryFileMissing');
    case 'stagger_hold':
      return t('productionRuns.detail.waiting.staggerHold');
    case 'spool_jam_recovering':
      return t('productionRuns.detail.waiting.spoolJamRecovering');
    case 'spool_jam_recovery_failed':
      return t('productionRuns.detail.waiting.spoolJamRecoveryFailed');
    case 'filament_runout_recovery_failed':
      return t('productionRuns.detail.waiting.filamentRunoutRecoveryFailed');
    case 'external_spool_runout':
      // The EXTERNAL spool holder ran dry, not an AMS slot. The distinction is
      // the whole point of the token: telling an operator to refill an AMS slot
      // here sends them to the wrong side of the machine.
      return t('productionRuns.detail.waiting.externalSpoolRunout');
    case 'external_feed_fault':
      // The EXTERNAL path failed to FEED (not "ran out"): no AMS is involved,
      // no swap will be attempted, and the fix is at the holder / PTFE tube.
      // Routing this through the AMS jam copy sends the operator to the wrong
      // side of the machine — the 003-H2S incident in miniature.
      return t('productionRuns.detail.waiting.externalFeedFault');
    case 'pinned_tray_unavailable':
      // An operator-pinned slot isn't loaded on this printer. Deliberately NOT
      // "low filament": nothing is short, the instruction simply can't be
      // honoured here, and the operator's two real options (load that tray, or
      // drop the pin) are both invisible under the deficit wording.
      return t('productionRuns.detail.waiting.pinnedTrayUnavailable');
    case 'spool_physical_fault':
      return t('productionRuns.detail.waiting.spoolPhysicalFault');
    default:
      // An unmapped token gets humanized (never shown raw to an operator); a
      // backend-authored sentence is already readable and passes through.
      return isTokenShaped(reason) ? humanizeToken(reason) : reason;
  }
}
