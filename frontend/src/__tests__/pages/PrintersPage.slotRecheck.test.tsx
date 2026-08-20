/**
 * WS11 — "Re-check slot" (doctrine rule 12, incident shape 32).
 *
 * The control this replaces ("Re-read RFID") could not conclude anything on a
 * tagless slot and said nothing when it didn't: an operator clicked it for 21
 * minutes against silence. So the load-bearing assertion in this file is not
 * that the happy path works — it is that EVERY verdict, including the two that
 * change nothing (`unchanged`, `empty`), puts a sentence on screen.
 *
 * Covered here:
 *   - the six verdict sentences, asserted as literal text;
 *   - the acknowledgement's undo action on a `minted` verdict;
 *   - the same undo on the slot card itself, driven by the backend's derived
 *     `recheck_undo_available` (WCAG 2.2 2.2.1 — a timed toast must never be an
 *     action's only path), and its disappearance once the undo lands.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

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

/** IDLE, nothing fed (tray_now omitted) and no dry cycle: the re-check verb is
 *  offered rather than blocked by the disabled-reason ladder. */
const status = {
  connected: true,
  state: 'IDLE',
  awaiting_plate_clear: false,
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
  ams_status_main: 0,
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PLA', tray_color: 'FF0000FF', tray_sub_brands: 'PLA Basic', state: 10 },
        { id: 1, tray_type: 'PETG', tray_color: '00FF00FF', tray_sub_brands: 'PETG HF', state: 10 },
      ],
    },
  ],
};

/** The slot under test: AMS 0, tray 1 — "Slot 2" everywhere in the UI. */
const AMS_ID = 0;
const TRAY_ID = 1;
const SLOT_LABEL = TRAY_ID + 1;

function recheckResponse(overrides: Record<string, unknown>) {
  return {
    verdict: 'unchanged',
    printer_id: printer.id,
    ams_id: AMS_ID,
    tray_id: TRAY_ID,
    spool_id: null,
    label_weight_g: null,
    brand: null,
    material: null,
    undo_available: false,
    ...overrides,
  };
}

function assignmentRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    spool_id: 42,
    printer_id: printer.id,
    printer_name: printer.name,
    ams_id: AMS_ID,
    tray_id: TRAY_ID,
    fingerprint_color: null,
    fingerprint_type: null,
    created_at: '2024-01-01T00:00:00Z',
    spool: {
      id: 42,
      material: 'PETG',
      brand: 'Foundi',
      color_name: 'Green',
      label_weight: 1000,
      weight_used: 120,
      rgba: '00FF00FF',
    },
    ...overrides,
  };
}

/** Open the hover card over a slot and return a scope for its contents. */
async function openSlotCard(index: number) {
  const slots = await screen.findAllByTestId('filament-slot');
  fireEvent.mouseEnter(slots[index]);
  // The card waits 80 ms before showing, to swallow pointer fly-overs.
  return waitFor(() => {
    const card = document.querySelector('.fixed.z-\\[60\\] > div');
    expect(card).not.toBeNull();
    return card as HTMLElement;
  });
}

beforeEach(() => {
  localStorage.removeItem('printerCardSize');
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
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

describe('PrintersPage — "Re-check slot" verdicts', () => {
  it.each([
    [
      'unchanged',
      {},
      `Slot ${SLOT_LABEL} unchanged. No roll movement recorded since the last check.`,
    ],
    [
      'minted',
      { verdict: 'minted', spool_id: 99, label_weight_g: 1000, undo_available: false },
      `Slot ${SLOT_LABEL}: new roll recorded. ${(1000).toLocaleString()} g assumed.`,
    ],
    [
      'identified',
      { verdict: 'identified', brand: 'Bambu Lab', material: 'PETG' },
      `Slot ${SLOT_LABEL}: Bambu Lab PETG recorded from its tag.`,
    ],
    [
      'queued',
      { verdict: 'queued' },
      `Slot ${SLOT_LABEL} re-check queued. The farm reads the tag at the next idle window and records the roll then.`,
    ],
    ['empty', { verdict: 'empty' }, `Slot ${SLOT_LABEL} is empty. Load a roll, then re-check.`],
    [
      'restored',
      { verdict: 'restored', spool_id: 42 },
      `Slot ${SLOT_LABEL}: previous roll restored.`,
    ],
  ])('verdict "%s" is announced as its own sentence', async (_verdict, overrides, sentence) => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(recheckResponse(overrides)),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    expect(await screen.findByText(sentence)).toBeInTheDocument();
  });

  it('falls back to a tag-read sentence when the tag names neither brand nor material', async () => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(recheckResponse({ verdict: 'identified' })),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    expect(await screen.findByText(`Slot ${SLOT_LABEL}: tag read.`)).toBeInTheDocument();
  });

  it('offers "Restore previous roll" on the acknowledgement of a mint', async () => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(
          recheckResponse({
            verdict: 'minted',
            spool_id: 99,
            label_weight_g: 1000,
            undo_available: true,
          }),
        ),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    const toast = await screen.findByText(
      `Slot ${SLOT_LABEL}: new roll recorded. ${(1000).toLocaleString()} g assumed.`,
    );
    const toastRow = toast.closest('div') as HTMLElement;
    expect(
      within(toastRow).getByRole('button', { name: 'Restore previous roll' }),
    ).toBeInTheDocument();
  });
});

describe('PrintersPage — standing undo on the slot card', () => {
  it('renders "Restore previous roll" while the offer stands, and drops it once undone', async () => {
    let undone = false;
    server.use(
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_undo_available: !undone })]),
      ),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck/undo`, () => {
        undone = true;
        return HttpResponse.json(recheckResponse({ verdict: 'restored', spool_id: 42 }));
      }),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    const undo = await within(card).findByRole('button', { name: 'Restore previous roll' });
    // An offer, not an interruption: it never takes focus for itself.
    expect(undo).not.toHaveFocus();

    await userEvent.click(undo);

    expect(
      await screen.findByText(`Slot ${SLOT_LABEL}: previous roll restored.`),
    ).toBeInTheDocument();
    expect(undone).toBe(true);
    await waitFor(() => {
      expect(
        within(card).queryByRole('button', { name: 'Restore previous roll' }),
      ).not.toBeInTheDocument();
    });
    // …and the card is still the open card, not a torn-down one: the verb
    // disappeared because the offer lapsed, not because the surface vanished.
    expect(within(card).getByRole('button', { name: 'Load' })).toBeInTheDocument();
  });

  it('names the reason when the standing offer is already gone (409)', async () => {
    server.use(
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_undo_available: true })]),
      ),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck/undo`, () =>
        HttpResponse.json({ detail: 'no_offer' }, { status: 409 }),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(
      await within(card).findByRole('button', { name: 'Restore previous roll' }),
    );

    expect(
      await screen.findByText(`Slot ${SLOT_LABEL}: no re-check to undo.`),
    ).toBeInTheDocument();
  });
});
