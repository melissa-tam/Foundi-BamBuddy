/**
 * waitingReasonText (Phase 3, F2): the single implementation that maps farm
 * waiting-reason machine codes to i18n keys, humanizes any unmapped bare token
 * (so an operator never sees `stagger_hold`-style raw text), and passes an
 * already-human backend sentence through verbatim. Shared by the run detail
 * page, the printer-card chip and the queue rows.
 */
import { describe, it, expect } from 'vitest';
import { waitingReasonText, humanizeToken, isTokenShaped } from '../../utils/waitingReason';

// A fake translator that echoes the key, so we can assert the code→key mapping
// without pulling in the i18n runtime.
const t = (k: string) => k;

describe('waitingReasonText', () => {
  it('returns null for a null reason', () => {
    expect(waitingReasonText(null, t)).toBeNull();
  });

  it('maps known machine codes to their (existing) i18n keys', () => {
    expect(waitingReasonText('printer_offline_stalled', t)).toBe(
      'productionRuns.detail.waiting.printerOfflineStalled',
    );
    expect(waitingReasonText('plate_not_empty_printer_detected', t)).toBe(
      'productionRuns.detail.waiting.visionHold',
    );
    expect(waitingReasonText('previous_print_failed', t)).toBe(
      'productionRuns.detail.waiting.previousPrintFailed',
    );
    expect(waitingReasonText('filament_short', t)).toBe('productionRuns.detail.waiting.filamentShort');
    expect(waitingReasonText('no_usb_drive', t)).toBe('productionRuns.detail.waiting.no_usb_drive');
    expect(waitingReasonText('stagger_hold', t)).toBe('productionRuns.detail.waiting.staggerHold');
    expect(waitingReasonText('spool_jam_recovering', t)).toBe(
      'productionRuns.detail.waiting.spoolJamRecovering',
    );
    expect(waitingReasonText('spool_jam_recovery_failed', t)).toBe(
      'productionRuns.detail.waiting.spoolJamRecoveryFailed',
    );
    expect(waitingReasonText('filament_runout_recovery_failed', t)).toBe(
      'productionRuns.detail.waiting.filamentRunoutRecoveryFailed',
    );
  });

  it('humanizes an unmapped bare token instead of returning it raw', () => {
    // Not in the tier-1 map → falls through to the humanizer.
    expect(waitingReasonText('some_new_hold', t)).toBe('Some new hold');
    expect(waitingReasonText('printer_wedged', t)).toBe('Printer wedged');
  });

  it('passes an unknown (already human-readable) reason through verbatim', () => {
    expect(waitingReasonText('Nozzle 0.4 != required 0.6', t)).toBe('Nozzle 0.4 != required 0.6');
  });
});

describe('humanizeToken', () => {
  it('turns a snake_case token into sentence-case copy', () => {
    expect(humanizeToken('stagger_hold')).toBe('Stagger hold');
    expect(humanizeToken('printer_offline_stalled')).toBe('Printer offline stalled');
    expect(humanizeToken('operator')).toBe('Operator');
  });

  it('leaves a token that humanizes to nothing untouched', () => {
    expect(humanizeToken('_')).toBe('_');
  });
});

describe('isTokenShaped', () => {
  it('accepts bare lowercase/underscore/digit tokens', () => {
    expect(isTokenShaped('stagger_hold')).toBe(true);
    expect(isTokenShaped('no_usb_drive')).toBe(true);
    expect(isTokenShaped('code2')).toBe(true);
  });

  it('rejects human sentences with spaces, punctuation or capitals', () => {
    expect(isTokenShaped('Nozzle 0.4 != required 0.6')).toBe(false);
    expect(isTokenShaped('Waiting for filament: 260 g on spool')).toBe(false);
    expect(isTokenShaped('Busy')).toBe(false);
  });
});
