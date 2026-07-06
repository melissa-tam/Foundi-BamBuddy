/**
 * Tests for the farm auto-recovery quarantine surfacing on the Printers page
 * (Phase 3): a quarantined printer shows a badge + reason, and the
 * "Clear quarantine" action is gated behind a confirmation before it POSTs.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const quarantinedPrinter = {
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
  quarantined: true,
  quarantine_reason: '3 consecutive dispatch failures',
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const idleStatus = {
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
};

describe('PrintersPage quarantine surfacing', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([quarantinedPrinter])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(idleStatus)),
      http.get('/api/v1/settings/', () => HttpResponse.json({ require_plate_clear: true })),
      http.get('/api/v1/settings/ui-preferences', () =>
        HttpResponse.json({ require_plate_clear: true }),
      ),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
    );
  });

  it('shows the quarantine badge and reason', async () => {
    render(<PrintersPage />);

    expect(await screen.findByText('Quarantined')).toBeInTheDocument();
    expect(screen.getByText('3 consecutive dispatch failures')).toBeInTheDocument();
  });

  it('gates clear-quarantine behind a confirmation, then POSTs', async () => {
    let clearCalled = false;
    server.use(
      http.post('/api/v1/printers/:id/clear-quarantine', () => {
        clearCalled = true;
        return HttpResponse.json({ ...quarantinedPrinter, quarantined: false, quarantine_reason: null });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);

    await screen.findByText('Quarantined');

    // The card-level "Clear quarantine" button opens the confirmation; no POST yet.
    const clearButtons = screen.getAllByRole('button', { name: /clear quarantine/i });
    await user.click(clearButtons[0]);

    expect(await screen.findByText('Clear quarantine?')).toBeInTheDocument();
    expect(clearCalled).toBe(false);

    // The confirm modal's confirm button carries the same label; it is the last
    // matching button in the DOM (modal overlays the card).
    const confirmButtons = screen.getAllByRole('button', { name: /clear quarantine/i });
    await user.click(confirmButtons[confirmButtons.length - 1]);

    await waitFor(() => expect(clearCalled).toBe(true));
  });
});
