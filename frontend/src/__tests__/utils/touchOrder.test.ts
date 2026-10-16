/**
 * The touch view's order: attention first (the fixed rank), then name, and the held
 * order a touched screen keeps; plus which tiles carry a camera still.
 */
import { describe, expect, it } from 'vitest';
import type { HoldChipState, Printer, PrinterStatus } from '../../api/client';
import {
  ATTENTION_ORDER,
  attentionOf,
  holdOrder,
  stillWanted,
  touchOrder,
  type TouchEntry,
} from '../../utils/touchOrder';

function printer(id: number, name: string, extra: Partial<Printer> = {}): Printer {
  return {
    id,
    name,
    serial_number: `SN${id}`,
    ip_address: '127.0.0.1',
    model: 'H2S',
    location: null,
    nozzle_count: 1,
    is_active: true,
    auto_archive: true,
    external_camera_url: null,
    external_camera_type: null,
    external_camera_enabled: false,
    external_camera_snapshot_url: null,
    camera_rotation: 0,
    plate_detection_enabled: false,
    quarantined: false,
    quarantine_reason: null,
    created_at: '',
    updated_at: '',
    ...extra,
  };
}

function status(extra: Partial<PrinterStatus> = {}): PrinterStatus {
  return { connected: true, state: 'IDLE', hms_errors: [], awaiting_plate_clear: false, ...extra } as PrinterStatus;
}

const chip = (tone: HoldChipState['tone']): HoldChipState => ({
  variant: 'incident',
  tone,
  label: { key: 'printers.incident.runout', values: {} },
  tooltip: [],
  qualifier: null,
});

const entry = (p: Printer, s?: Partial<PrinterStatus>): TouchEntry => ({ printer: p, status: s ? status(s) : undefined });

describe('attentionOf', () => {
  it('reads each reason off the server payload', () => {
    expect(attentionOf(entry(printer(1, 'a'), { hold_chip: chip('held') }))).toBe('held');
    expect(attentionOf(entry(printer(1, 'a'), { hold_chip: chip('acting') }))).toBe('acting');
    expect(attentionOf(entry(printer(1, 'a'), { awaiting_plate_clear: true }))).toBe('plateGated');
    expect(attentionOf(entry(printer(1, 'a'), { state: 'PAUSE' }))).toBe('paused');
    expect(attentionOf(entry(printer(1, 'a', { quarantined: true }), {}))).toBe('error');
    expect(
      attentionOf(entry(printer(1, 'a'), { hms_errors: [{ code: 'x', attr: 0, module: 0, severity: 1 }] })),
    ).toBe('error');
    expect(attentionOf(entry(printer(1, 'a', { service_hold: { since: '2026-10-16T00:00:00' } }), {}))).toBe(
      'maintenance',
    );
    expect(
      attentionOf(entry(printer(1, 'a'), { awaiting_plate_clear: true, eject_watch: { threshold_c: 30, hold_z: null, deferred: false } })),
    ).toBe('cooldown');
    expect(attentionOf(entry(printer(1, 'a'), { state: 'RUNNING' }))).toBeNull();
  });

  it('names the most urgent reason when several hold', () => {
    expect(
      attentionOf(entry(printer(1, 'a', { service_hold: { since: 'x' } }), { hold_chip: chip('acting'), state: 'PAUSE' })),
    ).toBe('acting');
  });
});

describe('touchOrder', () => {
  it('puts attention first in the fixed rank, then the rest by name (numeric-aware)', () => {
    const entries: TouchEntry[] = [
      entry(printer(1, '010-H2S'), { state: 'RUNNING' }),
      entry(printer(2, '002-H2S'), { state: 'IDLE' }),
      entry(printer(3, '009-H2S'), { state: 'PAUSE' }),
      entry(printer(4, '004-H2S'), { hold_chip: chip('held') }),
      entry(printer(5, '005-H2S'), { awaiting_plate_clear: true }),
      entry(printer(6, '006-H2S', { service_hold: { since: 'x' } }), {}),
      entry(printer(7, '007-H2C'), { hold_chip: chip('acting') }),
    ];
    expect(touchOrder(entries)).toEqual([4, 7, 5, 3, 6, 2, 1]);
  });

  it('keeps the rank complete: every reason sorts before a printer with none', () => {
    expect(ATTENTION_ORDER).toEqual(['held', 'acting', 'plateGated', 'paused', 'error', 'maintenance', 'cooldown']);
  });

  it('breaks a name tie by id, so the order is total', () => {
    expect(touchOrder([entry(printer(9, 'same')), entry(printer(3, 'same'))])).toEqual([3, 9]);
  });
});

describe('holdOrder (stable while touched)', () => {
  it('keeps the shown order even when the fresh sort moved', () => {
    expect(holdOrder([1, 2, 3], [3, 1, 2])).toEqual([1, 2, 3]);
  });

  it('appends a printer that appeared and drops one that went', () => {
    expect(holdOrder([1, 2, 3], [4, 3, 1])).toEqual([1, 3, 4]);
  });
});

describe('stillWanted', () => {
  it('is on for error, plate not empty, cooling and maintenance only', () => {
    expect(stillWanted(entry(printer(1, 'a'), { hold_chip: chip('acting') }))).toBe(true);
    expect(stillWanted(entry(printer(1, 'a'), { awaiting_plate_clear: true }))).toBe(true);
    expect(stillWanted(entry(printer(1, 'a'), { eject_watch: { threshold_c: null, hold_z: null, deferred: false } }))).toBe(true);
    expect(stillWanted(entry(printer(1, 'a', { service_hold: { since: 'x' } }), {}))).toBe(true);
    expect(stillWanted(entry(printer(1, 'a'), { state: 'RUNNING' }))).toBe(false);
    expect(stillWanted(entry(printer(1, 'a'), { state: 'IDLE' }))).toBe(false);
  });

  it('is off for a printer with no session (nothing to capture)', () => {
    expect(stillWanted(entry(printer(1, 'a'), { connected: false, awaiting_plate_clear: true }))).toBe(false);
    expect(stillWanted(entry(printer(1, 'a', { service_hold: { since: 'x' } })))).toBe(false);
  });
});
