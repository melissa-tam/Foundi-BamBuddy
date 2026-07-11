/**
 * Phase 1 (P1-B): the clear-plate affordance must appear whenever a printer's
 * awaiting_plate_clear flag is set — even when the global require_plate_clear
 * convenience toggle is OFF. The plate gate now blocks dispatch unconditionally,
 * so the operator always needs the recovery control when the gate is raised.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
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

const statusWith = (awaiting: boolean) => ({
  connected: true,
  state: 'FINISH',
  awaiting_plate_clear: awaiting,
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
});

function mountWith({ awaiting }: { awaiting: boolean }) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(statusWith(awaiting))),
    // The KEY condition: the global plate-clear toggle is OFF.
    http.get('/api/v1/settings/', () => HttpResponse.json({ require_plate_clear: false })),
    http.get('/api/v1/settings/ui-preferences', () =>
      HttpResponse.json({ require_plate_clear: false }),
    ),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
}

describe('PrintersPage clear-plate affordance (global toggle off)', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
  });

  it('renders the clear-plate control when awaiting_plate_clear is true even though require_plate_clear is off', async () => {
    mountWith({ awaiting: true });
    render(<PrintersPage />);

    expect(
      await screen.findByRole('button', { name: /mark plate as cleared/i }),
    ).toBeInTheDocument();
  });

  it('does not render the clear-plate control when the gate is not raised', async () => {
    mountWith({ awaiting: false });
    render(<PrintersPage />);

    // Wait for the card to mount, then assert the control is absent.
    await screen.findByText('H2S-Alpha');
    expect(
      screen.queryByRole('button', { name: /mark plate as cleared/i }),
    ).not.toBeInTheDocument();
  });
});
