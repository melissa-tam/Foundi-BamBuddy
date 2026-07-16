/**
 * Device-vs-declared model reconciliation surfacing on the Printers page
 * (farm Phase 2):
 * - A printer whose live status reports `model_mismatch` shows a warning
 *   banner with the reason and the dispatch-blocked explanation.
 * - The add-printer model dropdown shows the registry-driven "eject not
 *   qualified" hint for models whose geometry row is missing or not
 *   hardware-validated, and hides it for validated models.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

const printer = {
  id: 1,
  name: 'H2S-Alpha',
  ip_address: '192.168.2.50',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'H2S',
  is_active: true,
  auto_archive: true,
  nozzle_count: 1,
  location: 'Farm',
  quarantined: false,
  quarantine_reason: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const baseStatus = {
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

const geometryHandler = http.get('*/api/v1/model-geometry', () =>
  HttpResponse.json({
    geometries: [
      {
        model_key: 'H2S',
        bed_x: 340,
        bed_y: 320,
        env_x_min: 0,
        env_x_max: 340,
        env_y_min: -16,
        env_y_max: 325,
        max_part_height_mm: 42,
        z_travel_mm: 340,
        validated: true,
        notes: null,
        updated_at: '2026-07-01T00:00:00Z',
      },
      {
        model_key: 'H2C',
        bed_x: 330,
        bed_y: 320,
        env_x_min: 25,
        env_x_max: 325,
        env_y_min: 0,
        env_y_max: 320,
        max_part_height_mm: 42,
        z_travel_mm: 325,
        validated: false,
        notes: null,
        updated_at: '2026-07-01T00:00:00Z',
      },
    ],
    sweep_band_min_width_mm: 10,
  }),
);

describe('PrintersPage model mismatch + eject qualification', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
      http.get('/api/v1/settings/', () => HttpResponse.json({ require_plate_clear: true })),
      http.get('/api/v1/settings/ui-preferences', () =>
        HttpResponse.json({ require_plate_clear: true }),
      ),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      geometryHandler,
    );
  });

  it('shows the mismatch banner with reason when status reports model_mismatch', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({
          ...baseStatus,
          model_mismatch: true,
          model_mismatch_reason: "declared model 'H2S' but device reports 'H2C'",
        }),
      ),
    );

    render(<PrintersPage />);

    expect(await screen.findByText('Model mismatch')).toBeInTheDocument();
    expect(
      screen.getByText("declared model 'H2S' but device reports 'H2C'"),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Dispatch is blocked until the declared model is corrected.'),
    ).toBeInTheDocument();
  });

  it('shows no banner when the status carries no mismatch', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(baseStatus)),
    );

    render(<PrintersPage />);

    expect(await screen.findByText('H2S-Alpha')).toBeInTheDocument();
    expect(screen.queryByText('Model mismatch')).not.toBeInTheDocument();
  });

  it('hints "eject not qualified" in the add-printer model dropdown for unvalidated or unknown models', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(baseStatus)),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');

    await user.click(screen.getByRole('button', { name: /add printer/i }));

    // The add-modal model <select> is the one offering the H2 series options.
    const modelSelect = Array.from(document.querySelectorAll('select')).find((s) =>
      Array.from(s.options).some((o) => o.value === 'H2C'),
    );
    expect(modelSelect).toBeDefined();

    // Unvalidated registry row (H2C) -> hint shown.
    fireEvent.change(modelSelect!, { target: { value: 'H2C' } });
    expect(
      await screen.findByText(/auto-eject is not hardware-qualified for this model yet/i),
    ).toBeInTheDocument();

    // No registry row at all (X1C) -> hint shown too.
    fireEvent.change(modelSelect!, { target: { value: 'X1C' } });
    expect(
      await screen.findByText(/auto-eject is not hardware-qualified for this model yet/i),
    ).toBeInTheDocument();

    // Hardware-validated model (H2S) -> no hint.
    fireEvent.change(modelSelect!, { target: { value: 'H2S' } });
    await waitFor(() =>
      expect(
        screen.queryByText(/auto-eject is not hardware-qualified for this model yet/i),
      ).not.toBeInTheDocument(),
    );
  });
});
