/**
 * `utils/productionRuns` — the pure derivations behind the run badges.
 *
 * `hasLiveBlockedPrinters` is the load-bearing one: it gates BOTH the
 * blocked-printers chip and the eligibility panel the chip discloses, and it
 * reads two sources on purpose. The backend's list flag `has_blocked_printers`
 * is computed WITHOUT the filament-short / no-USB / capability dimensions —
 * those exist only on the detail response — so a predicate that trusted the
 * flag alone hid exactly the printers the panel was about to list.
 */
import { describe, it, expect } from 'vitest';
import {
  eligibilityReasons,
  hasLiveBlockedPrinters,
  isScheduled,
} from '../../utils/productionRuns';
import type { ProductionRun, RunPrinterState } from '../../types/productionRuns';

function printerState(overrides: Partial<RunPrinterState> = {}): RunPrinterState {
  return {
    printer_id: 1,
    name: 'H2S-Alpha',
    connected: true,
    quarantined: false,
    awaiting_plate_clear: false,
    model_mismatch: false,
    model_mismatch_reason: null,
    stalled: false,
    vision_hold: false,
    filament_short_live: false,
    filament_short_detail: null,
    no_usb_drive: false,
    capability_reason: null,
    ...overrides,
  };
}

type RunFlags = Pick<ProductionRun, 'has_blocked_printers' | 'status'>;
const runFlags = (overrides: Partial<RunFlags> = {}): RunFlags => ({
  has_blocked_printers: false,
  status: 'active',
  ...overrides,
});

describe('eligibilityReasons', () => {
  it('is empty for an eligible printer', () => {
    expect(eligibilityReasons(printerState())).toEqual([]);
  });

  it('reports each blocking flag, in display order', () => {
    const reasons = eligibilityReasons(
      printerState({
        connected: false,
        quarantined: true,
        awaiting_plate_clear: true,
        model_mismatch: true,
        model_mismatch_reason: 'sliced for H2C',
        filament_short_live: true,
        filament_short_detail: 'needs 455 g, 260 g on spool',
        no_usb_drive: true,
        capability_reason: 'Nozzle 0.4 != required 0.6',
      }),
    );

    expect(reasons).toEqual([
      { kind: 'offline' },
      { kind: 'quarantined' },
      { kind: 'awaitingPlateClear' },
      { kind: 'modelMismatch', detail: 'sliced for H2C' },
      { kind: 'filamentShort', detail: 'needs 455 g, 260 g on spool' },
      { kind: 'noUsbDrive' },
      { kind: 'capability', detail: 'Nozzle 0.4 != required 0.6' },
    ]);
  });

  it('ignores the self-resolving states', () => {
    // Busy / stagger-hold are not modelled; stalled and vision_hold feed the
    // backend's list flag but are deliberately not panel reasons.
    expect(eligibilityReasons(printerState({ stalled: true, vision_hold: true }))).toEqual([]);
  });
});

describe('hasLiveBlockedPrinters', () => {
  it('trusts the list flag when no printer states are in hand', () => {
    expect(hasLiveBlockedPrinters(runFlags({ has_blocked_printers: true }))).toBe(true);
    expect(hasLiveBlockedPrinters(runFlags())).toBe(false);
  });

  it('catches a detail-only block the list flag misses', () => {
    const states = [printerState({ filament_short_live: true })];
    // The regression this pair exists for: flag false, panel non-empty.
    expect(hasLiveBlockedPrinters(runFlags(), states)).toBe(true);
    expect(hasLiveBlockedPrinters(runFlags())).toBe(false);
  });

  it('stays false when every printer in the detail is eligible', () => {
    expect(hasLiveBlockedPrinters(runFlags(), [printerState(), printerState({ printer_id: 2 })])).toBe(
      false,
    );
  });

  it('is false for a terminal run whichever source says otherwise', () => {
    const states = [printerState({ no_usb_drive: true })];
    for (const status of ['completed', 'cancelled'] as const) {
      expect(hasLiveBlockedPrinters(runFlags({ has_blocked_printers: true, status }), states)).toBe(
        false,
      );
    }
  });

  it('treats a paused run as live', () => {
    expect(hasLiveBlockedPrinters(runFlags({ has_blocked_printers: true, status: 'paused' }))).toBe(
      true,
    );
  });
});

describe('isScheduled', () => {
  it('is true only for an active run whose start is still ahead', () => {
    const future = new Date(Date.now() + 3600_000).toISOString();
    const past = new Date(Date.now() - 3600_000).toISOString();
    expect(isScheduled({ status: 'active', scheduled_start_at: future })).toBe(true);
    expect(isScheduled({ status: 'active', scheduled_start_at: past })).toBe(false);
    expect(isScheduled({ status: 'active', scheduled_start_at: null })).toBe(false);
    // The operator's hold wins over the deferred-start window.
    expect(isScheduled({ status: 'paused', scheduled_start_at: future })).toBe(false);
  });
});
