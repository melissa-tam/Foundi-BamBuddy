/**
 * The touch tile's contextual primary buttons, one context at a time (plan item 15).
 */
import { describe, expect, it } from 'vitest';
import type { Printer, PrinterStatus } from '../../api/client';
import { tilePrimaryActions } from '../../utils/touchActions';

const base: Printer = {
  id: 1,
  name: '001-H2S',
  serial_number: 'SN1',
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
};

const status = (extra: Partial<PrinterStatus>): PrinterStatus =>
  ({ connected: true, state: 'IDLE', hms_errors: [], awaiting_plate_clear: false, ...extra }) as PrinterStatus;

describe('tilePrimaryActions', () => {
  it('plate gated -> Mark plate cleared', () => {
    expect(tilePrimaryActions(base, status({ state: 'FINISH', awaiting_plate_clear: true }))).toEqual(['markCleared']);
  });

  it('a gate under the farm cooldown watch offers no Mark plate cleared (it would cancel the eject)', () => {
    expect(
      tilePrimaryActions(
        base,
        status({ state: 'FINISH', awaiting_plate_clear: true, eject_watch: { threshold_c: 27, hold_z: null, deferred: false } }),
      ),
    ).toEqual([]);
  });

  it('plate-check human turn -> Ignore and resume + Stop', () => {
    expect(
      tilePrimaryActions(
        base,
        status({
          state: 'PAUSE',
          plate_check_exit: { print_error: '0500808C', job_id: '1', actions: ['IGNORE_RESUME'], deadline_at: null },
        }),
      ),
    ).toEqual(['ignoreResume', 'stop']);
  });

  it('paused -> Resume', () => {
    expect(tilePrimaryActions(base, status({ state: 'PAUSE' }))).toEqual(['resume']);
  });

  it('quarantined -> Recover', () => {
    expect(tilePrimaryActions({ ...base, quarantined: true }, status({}))).toEqual(['recover']);
  });

  it('a fault whose exit is Recover -> Recover', () => {
    expect(
      tilePrimaryActions(
        base,
        status({
          open_incident: {
            id: 1,
            kind: 'z_reference_lost',
            status: 'escalated',
            driver_live: false,
            slot_desc: null,
            created_at: null,
            operator_exits: true,
            printer_messages: [],
          },
        }),
      ),
    ).toEqual(['recover']);
  });

  it('a bare plate gate leaves Recover to More', () => {
    const gated = status({
      state: 'FINISH',
      awaiting_plate_clear: true,
      occupancy: { plate: { occupied: true, source_subtask_id: null, policy: null, since: null, refusal: null }, eject: null, lease_age_s: null },
    });
    expect(tilePrimaryActions(base, gated)).toEqual(['markCleared']);
  });

  it('maintenance -> Exit maintenance', () => {
    expect(tilePrimaryActions({ ...base, service_hold: { since: 'x' } }, status({}))).toEqual(['exitMaintenance']);
  });

  it('printing -> Camera', () => {
    expect(tilePrimaryActions(base, status({ state: 'RUNNING' }))).toEqual(['camera']);
  });

  it('an idle printer gets More only', () => {
    expect(tilePrimaryActions(base, status({ state: 'IDLE' }))).toEqual([]);
  });

  it('caps at two, most pressing first', () => {
    const many = tilePrimaryActions(
      { ...base, quarantined: true, service_hold: { since: 'x' } },
      status({ state: 'PAUSE' }),
    );
    expect(many).toEqual(['exitMaintenance', 'resume']);
  });
});
