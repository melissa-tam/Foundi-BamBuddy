/**
 * The printer card's slot rings are the backend's answer (`status.toolhead`,
 * built by `printer_manager.toolhead_payload` from the one feed-state owner),
 * never a re-derivation from `tray_now` / `last_loaded_tray`:
 *
 *   - the green ring sits on `toolhead.active_tray`. During a firmware runout
 *     auto-switch `tray_now` pre-flips to the backup slot minutes before the
 *     backup feeds, while the drained roll's tail still feeds (2026-10-10:
 *     005/001/006-H2S); the ring stays on the draining roll;
 *   - the dimmed "was feeding" ring sits on `toolhead.was_feeding_tray`, set
 *     while a job is active and nothing feeds;
 *   - neither field names a tray (or no `toolhead` at all) → no ring.
 *
 * Slots are addressed by their accessible name (`ams.slotDialogLabel`). The
 * green ring has no accessible state of its own, so it is read off the slot
 * visual's ring class; the dimmed ring also carries its screen-reader marker.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { screen, within } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import en from '../../i18n/locales/en';
import { getAmsLabel } from '../../utils/amsHelpers';

const printer = {
  id: 1,
  name: 'H2S-Alpha',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'H2S',
  enabled: true,
  is_active: true,
  nozzle_diameter: 0.6,
  nozzle_type: 'hardened_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const loaded = { tray_type: 'PETG', tray_color: '000000FF', tray_sub_brands: 'PETG HF' };

/** One four-slot AMS mid auto-switch: slot 1 has drained at the AMS (its tail
 *  still feeds), slot 2 holds the firmware's backup, slots 3-4 are loaded. */
const baseStatus = {
  connected: true,
  state: 'RUNNING',
  awaiting_plate_clear: false,
  progress: 40,
  layer_num: 60,
  total_layers: 167,
  temperatures: { nozzle: 250, bed: 70, chamber: 30 },
  remaining_time: 3600,
  filename: null,
  wifi_signal: -50,
  hms_errors: [],
  ams_status_main: 0,
  vt_tray: [],
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: '', state: 9 },
        { id: 1, ...loaded, state: 11 },
        { id: 2, ...loaded, state: 10 },
        { id: 3, ...loaded, state: 10 },
      ],
    },
  ],
};

function serveStatus(overrides: Record<string, unknown>) {
  server.use(
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json({ ...baseStatus, ...overrides })),
  );
}

/** A slot's hover trigger, by its accessible name "<unit> slot <n>: <content>". */
function findSlot(unit: string, slot: number): Promise<HTMLElement> {
  const prefix = en.ams.slotDialogLabel
    .replace('{{ams}}', unit)
    .replace('{{slot}}', String(slot))
    .split('{{content}}')[0];
  return screen.findByRole('button', { name: (name) => name.startsWith(prefix) });
}

const AMS_A = getAmsLabel(0, 4);
const EXTERNAL = getAmsLabel(255, 1);

type Ring = 'active' | 'was-feeding' | 'none';

/** The ring the card draws on a slot's visual (the trigger's first child). */
function ringOf(trigger: HTMLElement): Ring {
  const visual = trigger.firstElementChild;
  if (visual?.classList.contains('ring-bambu-green')) return 'active';
  if (visual?.classList.contains('ring-bambu-green/40')) return 'was-feeding';
  return 'none';
}

async function ringsOfAmsA(): Promise<Ring[]> {
  const slots = await Promise.all([1, 2, 3, 4].map((n) => findSlot(AMS_A, n)));
  return slots.map(ringOf);
}

beforeEach(() => {
  localStorage.removeItem('printerCardSize');
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/settings/ui-preferences', () =>
      HttpResponse.json({
        ams_humidity_good: 40,
        ams_humidity_fair: 60,
        ams_temp_good: 30,
        ams_temp_fair: 35,
        require_plate_clear: true,
      }),
    ),
    http.get('/api/v1/spoolman/settings', () =>
      HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' }),
    ),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
  );
});

afterEach(() => {
  server.resetHandlers();
});

describe('PrintersPage — slot rings read the backend feed state', () => {
  it('keeps the green ring on the draining roll while tray_now has pre-flipped to the backup', async () => {
    serveStatus({
      tray_now: 1,
      last_loaded_tray: 1,
      toolhead: { feed: 'fed', active_tray: 0, was_feeding_tray: null, refill: null },
    });
    render(<PrintersPage />);

    expect(await ringsOfAmsA()).toEqual(['active', 'none', 'none', 'none']);
  });

  it('dims the ring on was_feeding_tray during a PAUSE with nothing fed', async () => {
    serveStatus({
      state: 'PAUSE',
      tray_now: 255,
      // Names another slot: the ring must not read it.
      last_loaded_tray: 0,
      toolhead: {
        feed: 'empty',
        active_tray: null,
        was_feeding_tray: 1,
        refill: null,
        refill_reason: 'runout_demand',
      },
    });
    render(<PrintersPage />);

    expect(await ringsOfAmsA()).toEqual(['none', 'was-feeding', 'none', 'none']);
    const wasFeeding = await findSlot(AMS_A, 2);
    expect(within(wasFeeding).getByText(en.printers.slot.wasFeeding)).toBeInTheDocument();
  });

  it.each([
    ['the toolhead names no tray', { toolhead: { feed: 'unknown', active_tray: null, was_feeding_tray: null, refill: null } }],
    ['the frame carries no toolhead', {}],
  ])('draws no ring when %s, whatever tray_now says', async (_label, toolhead) => {
    serveStatus({ tray_now: 1, last_loaded_tray: 1, ...toolhead });
    render(<PrintersPage />);

    expect(await ringsOfAmsA()).toEqual(['none', 'none', 'none', 'none']);
    expect(screen.queryByText(en.printers.slot.wasFeeding)).not.toBeInTheDocument();
  });

  it('rings the external spool when active_tray is 254', async () => {
    serveStatus({
      tray_now: 254,
      vt_tray: [{ id: 254, tray_type: 'PLA', tray_color: 'FFFFFFFF', tray_sub_brands: 'PLA Basic' }],
      toolhead: { feed: 'external', active_tray: 254, was_feeding_tray: null, refill: null },
    });
    render(<PrintersPage />);

    expect(ringOf(await findSlot(EXTERNAL, 1))).toBe('active');
    expect(await ringsOfAmsA()).toEqual(['none', 'none', 'none', 'none']);
  });
});
