/**
 * W2 manual "Eject now" + W3 cooldown tooltip on the Printers page.
 *
 * - The Eject now button appears beside mark-cleared whenever the plate gate is
 *   raised (same predicate as showClearPlateButton) and the user can control
 *   printers (auth-disabled test → hasPermission returns true).
 * - A `bed_hot` 409 opens the confirm dialog showing BOTH live temps; confirming
 *   re-calls the endpoint with allow_hot=true.
 * - While a cooldown eject watch is armed, the mark-cleared button carries the
 *   "marking clears cancels the pending auto-eject" hint (W3).
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 1,
  name: 'H2S-Alpha',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'H2S',
  is_active: true,
  auto_archive: true,
  nozzle_count: 1,
  location: 'Farm',
  quarantined: false,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

function statusFinish(extra: Record<string, unknown> = {}) {
  return {
    connected: true,
    state: 'FINISH',
    awaiting_plate_clear: true,
    progress: 0,
    layer_num: 0,
    total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 },
    remaining_time: 0,
    filename: null,
    wifi_signal: -50,
    vt_tray: [],
    ...extra,
  };
}

function mount(status: Record<string, unknown>) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/settings/', () => HttpResponse.json({ require_plate_clear: true })),
    http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ require_plate_clear: true })),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
}

describe('PrintersPage manual eject (W2) + cooldown tooltip (W3)', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize'); // default cardSize 2 → expanded
  });

  it('renders the Eject now button when the plate gate is raised', async () => {
    mount(statusFinish());
    render(<PrintersPage />);
    expect(await screen.findByRole('button', { name: /eject now/i })).toBeInTheDocument();
  });

  it('does not render Eject now when the gate is not raised', async () => {
    mount(statusFinish({ awaiting_plate_clear: false }));
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    expect(screen.queryByRole('button', { name: /eject now/i })).not.toBeInTheDocument();
  });

  it('opens the hot-bed confirm with live temps on a bed_hot 409, then re-calls with allow_hot=true', async () => {
    const ejectCalls: Array<{ allow_hot: boolean }> = [];
    mount(statusFinish());
    server.use(
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        const body = (await request.json()) as { allow_hot: boolean };
        ejectCalls.push(body);
        if (!body.allow_hot) {
          return HttpResponse.json(
            { detail: { code: 'bed_hot', bed_c: 45, threshold_c: 33 } },
            { status: 409 },
          );
        }
        return HttpResponse.json({ mode: 'dispatched' });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);

    const ejectBtn = await screen.findByRole('button', { name: /eject now/i });
    await user.click(ejectBtn);

    // Confirm dialog appears; the body shows BOTH the live bed and threshold.
    expect(await screen.findByText('Eject while bed is hot?')).toBeInTheDocument();
    const body = screen.getByText(/release threshold/i);
    expect(body.textContent).toContain('45');
    expect(body.textContent).toContain('33');
    await waitFor(() => expect(ejectCalls).toHaveLength(1));
    expect(ejectCalls[0].allow_hot).toBe(false);

    // Confirm → re-call with allow_hot=true (dialog confirm carries the same label).
    const ejectButtons = screen.getAllByRole('button', { name: /eject now/i });
    await user.click(ejectButtons[ejectButtons.length - 1]);
    await waitFor(() => expect(ejectCalls).toHaveLength(2));
    expect(ejectCalls[1].allow_hot).toBe(true);
  });

  it('shows the cancels-eject hint on the mark-cleared button while a cooldown watch is armed', async () => {
    mount(statusFinish({ eject_watch: { threshold_c: 33 } }));
    render(<PrintersPage />);
    expect(
      await screen.findByTitle('Marking the plate cleared cancels the pending auto-eject.'),
    ).toBeInTheDocument();
  });
});
