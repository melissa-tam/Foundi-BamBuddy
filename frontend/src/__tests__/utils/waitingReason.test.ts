/**
 * waitingReasonText (Phase 3, F2): the single implementation that maps farm
 * waiting-reason machine codes to i18n keys and passes already-human reasons
 * through verbatim. Shared by the run detail page and the printer-card chip.
 */
import { describe, it, expect } from 'vitest';
import { waitingReasonText } from '../../utils/waitingReason';

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
  });

  it('passes an unknown (already human-readable) reason through verbatim', () => {
    expect(waitingReasonText('Nozzle 0.4 != required 0.6', t)).toBe('Nozzle 0.4 != required 0.6');
  });
});
