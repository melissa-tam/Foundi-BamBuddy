/**
 * B4 slot-verb consolidation, driven through PrintersPage.
 *
 * Two verbs became one on each card, and both merges are load-bearing for the
 * operator rather than cosmetic:
 *  - an occupied slot with a bound row offers a single "New roll…" whatever the
 *    row's tag-ness, and the confirm posts the merged
 *    `POST /inventory/spools/{id}/new-roll` keyed by that row;
 *  - an empty slot carrying a stale binding offers "Unassign" — the same verb and
 *    the same `DELETE /inventory/assignments/…` the occupied card offers, which
 *    used to be called "Clear slot" purely because the slot read empty.
 */

import { describe, it, expect, beforeEach } from 'vitest';
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

/** Slot 0 holds a TAGGED Bambu roll; slot 1 reads empty. */
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
  ams: [
    {
      id: 0,
      humidity: 30,
      temp: 25,
      dry_time: 0,
      dry_sf_reason: [],
      module_type: 'ams',
      tray: [
        {
          id: 0,
          tray_type: 'PETG',
          tray_color: 'FF8800FF',
          tray_sub_brands: 'PETG HF',
          tray_info_idx: 'GFG99',
          tag_uid: 'DEADBEEF',
          tray_uuid: 'DEADBEEFDEADBEEFDEADBEEFDEADBEEF',
          remain: 40,
          state: 11,
        },
        { id: 1, state: 0 },
      ],
    },
  ],
};

/** The bound ledger row on slot 0 — TAGGED, which used to route to a second verb. */
const taggedAssignment = {
  id: 9,
  spool_id: 12,
  printer_id: 1,
  printer_name: 'H2S-Alpha',
  ams_id: 0,
  tray_id: 0,
  fingerprint_color: 'FF8800FF',
  fingerprint_type: 'PETG',
  created_at: '2026-08-01T00:00:00Z',
  present: true,
  spool: {
    id: 12,
    material: 'PETG',
    brand: 'Bambu Lab',
    color_name: 'Orange',
    rgba: 'FF8800FF',
    label_weight: 1000,
    weight_used: 960,
    tag_uid: 'DEADBEEF',
    tray_uuid: 'DEADBEEFDEADBEEFDEADBEEFDEADBEEF',
    archived_at: null,
    spent_at: null,
    created_at: '2026-08-01T00:00:00Z',
  },
};

/** A binding that outlived the filament on the EMPTY slot 1. */
const staleAssignment = {
  ...taggedAssignment,
  id: 10,
  spool_id: 44,
  tray_id: 1,
  present: false,
  spool: {
    ...taggedAssignment.spool,
    id: 44,
    brand: 'Overture',
    color_name: 'Black',
    tag_uid: null,
    tray_uuid: null,
  },
};

/** Hover-card visibility flips after an 80 ms timeout — wait it out. */
async function hoverSlot(slot: Element, expected: RegExp) {
  fireEvent.mouseEnter(slot);
  await waitFor(() => expect(screen.getByRole('button', { name: expected })).toBeInTheDocument());
}

describe('PrintersPage — merged slot verbs (B4)', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
      http.get('/api/v1/inventory/spools', () => HttpResponse.json([])),
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([taggedAssignment, staleAssignment]),
      ),
    );
  });

  it('offers ONE "New roll…" on a tagged bound row and posts it to /new-roll', async () => {
    const user = userEvent.setup();
    let captured: { path: string; body: unknown } | null = null;

    server.use(
      http.post('/api/v1/inventory/spools/:id/new-roll', async ({ params, request }) => {
        captured = { path: String(params.id), body: await request.json() };
        return HttpResponse.json({ id: 77, brand: 'Overture' });
      }),
    );

    render(<PrintersPage />);

    await waitFor(() => expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0));
    const slots = screen.getAllByTestId('filament-slot');
    await hoverSlot(slots[0], /New roll/i);

    // The merge is the assertion: a tagged row gets the same single verb, and the
    // verb it replaced is gone from the card entirely.
    expect(screen.getAllByRole('button', { name: /New roll/i })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /Re-spool tag/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /New roll/i }));

    // The form opens on the reused-tag framing, because the row carries a tag.
    const dialog = await screen.findByRole('dialog', { name: /Move this tag onto a new roll/i });
    await user.type(within(dialog).getByLabelText('Brand'), 'Overture');
    await user.click(within(dialog).getByRole('button', { name: /Re-spooled — fresh spool/i }));

    await waitFor(() => expect(captured).not.toBeNull());
    // Keyed by the BOUND row, carrying the slot it sits in.
    expect(captured!.path).toBe('12');
    expect(captured!.body).toMatchObject({
      printer_id: 1,
      ams_id: 0,
      tray_id: 0,
      brand: 'Overture',
    });
  });

  it('offers "Unassign" — not a card-specific "Clear slot" — on an empty slot with a stale binding', async () => {
    const user = userEvent.setup();
    let deleted: string | null = null;

    server.use(
      http.delete('/api/v1/inventory/assignments/:printerId/:amsId/:trayId', ({ params }) => {
        deleted = `${params.printerId}/${params.amsId}/${params.trayId}`;
        return HttpResponse.json({ success: true });
      }),
    );

    render(<PrintersPage />);

    // An EMPTY slot renders `EmptySlotHoverCard`, which carries no
    // `filament-slot` testid — reach it by its accessible name instead.
    const emptyTrigger = await screen.findByRole('button', { name: /slot 2:/i });
    fireEvent.mouseEnter(emptyTrigger);
    const unassign = await screen.findByRole('button', {
      name: /Unassign Overture PETG - Black from this slot/i,
    });
    expect(screen.queryByRole('button', { name: /^Clear slot$/i })).not.toBeInTheDocument();

    // The release is still gated behind the confirm dialog.
    await user.click(unassign);
    const confirm = await screen.findByRole('dialog', { name: /Unassign this spool/i });
    expect(deleted).toBeNull();

    await user.click(within(confirm).getByRole('button', { name: /^Unassign$/i }));
    await waitFor(() => expect(deleted).toBe('1/0/1'));
  });
});
