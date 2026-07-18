/**
 * Geometry-registry integration on the Eject Profiles page (farm eject,
 * Phase 2):
 * - The create/edit dialog derives validation bounds from the mocked
 *   /model-geometry API: sub-minimum or beyond-bed sweep bands are hard errors
 *   that block save; the cooldown ambient-trap and part-height-ceiling checks
 *   are non-blocking warnings.
 * - The preview panel resolves geometry from a model picker (defaulting to the
 *   first hardware-validated registry row), sends it as `model`, and surfaces
 *   the response's geometry warnings.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EjectProfilesPage } from '../../pages/EjectProfilesPage';

const geometryRows = [
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
];

function profile(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 1,
    name: 'Fast sweep',
    cooldown_temp_c: 28,
    clearance_mm: 10,
    z_offset_mm: 0.4,
    descent_steps: 4,
    x_passes: 11,
    x_margin_mm: 3,
    front_overhang_mm: 2,
    back_overhang_mm: 2,
    eject_speed_mm_min: 3000,
    skim_speed_mm_min: 1500,
    cooling_fan_assist: true,
    final_skim: true,
    max_part_height_mm: 42,
    sweep_x_min_mm: null,
    sweep_x_max_mm: null,
    sweep_start_frac: 1,
    bed_drop_clearance_mm: null,
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-01T10:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  server.use(
    http.get('*/api/v1/model-geometry', () =>
      HttpResponse.json({ geometries: geometryRows, sweep_band_min_width_mm: 10 }),
    ),
    http.get('*/api/v1/settings/', () =>
      HttpResponse.json({ farm_cooldown_warn_floor_c: 30 }),
    ),
    http.get('*/api/v1/library/files', () => HttpResponse.json([])),
  );
});

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

async function openCreateDialog(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('No eject profiles yet');
  await user.click(screen.getByRole('button', { name: /new profile/i }));
  await screen.findByRole('dialog');
}

describe('EjectProfilesPage geometry-derived form validation', () => {
  it('blocks save on a sub-minimum sweep band using the API band constant', async () => {
    let postCalled = false;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.post('*/api/v1/eject-profiles', () => {
        postCalled = true;
        return HttpResponse.json(profile(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    await user.type(screen.getByLabelText('Name'), 'banded');
    await user.click(screen.getByRole('switch', { name: 'Restrict X sweep band' }));
    await user.type(screen.getByLabelText('X band min (mm)'), '50');
    await user.type(screen.getByLabelText('X band max (mm)'), '55');

    // 55 - 50 < the server's sweep_band_min_width_mm (10, from the mocked API).
    expect(
      await screen.findByText('The sweep band must be at least 10 mm wide.'),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^save$/i }));
    expect(postCalled).toBe(false);
  });

  it('flags a band beyond the widest registered bed, and saves once fixed', async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.post('*/api/v1/eject-profiles', async ({ request }) => {
        postedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(profile({ id: 9, name: 'wide' }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    await user.type(screen.getByLabelText('Name'), 'wide');
    await user.click(screen.getByRole('switch', { name: 'Restrict X sweep band' }));
    await user.type(screen.getByLabelText('X band min (mm)'), '50');
    await user.type(screen.getByLabelText('X band max (mm)'), '400');

    // 400 > max(bed_x) = 340 (H2S row from the mocked registry).
    expect(
      await screen.findByText('X band max is beyond the widest registered bed (340 mm).'),
    ).toBeInTheDocument();

    // Fixing the band clears the error and the save goes through.
    await user.clear(screen.getByLabelText('X band max (mm)'));
    await user.type(screen.getByLabelText('X band max (mm)'), '200');
    await waitFor(() =>
      expect(
        screen.queryByText('X band max is beyond the widest registered bed (340 mm).'),
      ).not.toBeInTheDocument(),
    );

    await user.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedBody).toMatchObject({ sweep_x_min_mm: 50, sweep_x_max_mm: 200 });
  });

  it('warns (non-blocking) when the cooldown sits below the ambient-trap floor', async () => {
    server.use(http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])));

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    // Default cooldown is 28 °C, below the mocked farm_cooldown_warn_floor_c=30.
    expect(
      await screen.findByText(/below the cooldown warn floor \(30 °C\)/i),
    ).toBeInTheDocument();

    // Raising the threshold above the floor clears the warning.
    const cooldown = screen.getByLabelText('Cooldown temperature (°C)');
    await user.clear(cooldown);
    await user.type(cooldown, '33');
    await waitFor(() =>
      expect(screen.queryByText(/below the cooldown warn floor/i)).not.toBeInTheDocument(),
    );
  });

  it('warns when max part height exceeds every registered model ceiling', async () => {
    server.use(http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])));

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    // Default 42 sits exactly at the ceiling — no warning.
    expect(screen.queryByText(/height ceiling of every registered model/i)).not.toBeInTheDocument();

    const height = screen.getByLabelText('Max part height (mm)');
    await user.clear(height);
    await user.type(height, '60');

    expect(
      await screen.findByText(/height ceiling of every registered model \(max 42 mm\)/i),
    ).toBeInTheDocument();
  });

  it('reveals the bed-drop clearance input prefilled at 50 when the assist is toggled on', async () => {
    server.use(http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])));

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    // Hidden until the assist is enabled.
    expect(screen.queryByLabelText('Bottom clearance (mm)')).not.toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: 'Bed-drop release assist' }));

    const clearance = await screen.findByLabelText('Bottom clearance (mm)');
    expect(clearance).toHaveValue(50);
  });

  it('sends bed_drop_clearance_mm: null when the bed-drop assist stays off', async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.post('*/api/v1/eject-profiles', async ({ request }) => {
        postedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(profile({ id: 8, name: 'no drop' }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    await user.type(screen.getByLabelText('Name'), 'no drop');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedBody).toMatchObject({ bed_drop_clearance_mm: null });
  });

  it('sends bed_drop_clearance_mm: 50 when the bed-drop assist is toggled on', async () => {
    let postedBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.post('*/api/v1/eject-profiles', async ({ request }) => {
        postedBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(profile({ id: 9, name: 'with drop' }), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    await user.type(screen.getByLabelText('Name'), 'with drop');
    await user.click(screen.getByRole('switch', { name: 'Bed-drop release assist' }));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(postedBody).not.toBeNull());
    expect(postedBody).toMatchObject({ bed_drop_clearance_mm: 50 });
  });

  it('warns when the bed-drop assist is on but a model lacks a registered Z travel', async () => {
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.get('*/api/v1/model-geometry', () =>
        HttpResponse.json({
          geometries: [
            { ...geometryRows[0], z_travel_mm: 340 },
            { ...geometryRows[1], z_travel_mm: null },
          ],
          sweep_band_min_width_mm: 10,
        }),
      ),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    await openCreateDialog(user);

    // No warning until the assist is enabled.
    expect(screen.queryByText(/cannot generate this profile/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: 'Bed-drop release assist' }));

    // The warning names the model(s) missing z_travel_mm (H2C here).
    expect(
      await screen.findByText(
        /Models without a registered Z travel \(H2C\) cannot generate this profile/i,
      ),
    ).toBeInTheDocument();
  });
});

describe('EjectProfilesPage preview geometry resolution', () => {
  it('defaults the model picker to the validated row, sends it, and shows geometry warnings', async () => {
    let previewBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([profile()])),
      http.get('*/api/v1/library/files', () =>
        HttpResponse.json([
          { id: 5, filename: 'unit.gcode.3mf', file_type: 'gcode.3mf', file_size: 1 },
        ]),
      ),
      http.get('*/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
      http.post('*/api/v1/eject-profiles/:id/preview', async ({ request }) => {
        previewBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          gcode: '; ===== FARM EJECT BLOCK profile=Fast sweep =====',
          validation: { ok: true, errors: [], warnings: [] },
          max_z_height: 20,
          warnings: ["model 'H2C' geometry is not hardware-validated — run the hardware ladder"],
        });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    // The model picker defaults to the first VALIDATED registry row (H2S); the
    // unvalidated H2C option is labelled as such.
    const modelSelect = await screen.findByLabelText('Printer model');
    await waitFor(() => expect(modelSelect).toHaveValue('H2S'));
    expect(
      screen.getByRole('option', { name: 'H2C — not hardware-validated' }),
    ).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText('Library file'), '5');
    await user.click(screen.getByRole('button', { name: /generate preview/i }));

    await waitFor(() => expect(previewBody).not.toBeNull());
    expect(previewBody).toMatchObject({ library_file_id: 5, plate_index: 1, model: 'H2S' });

    // Geometry warnings from the response render in their own titled block.
    expect(await screen.findByText('Geometry warnings')).toBeInTheDocument();
    expect(
      screen.getByText(/not hardware-validated — run the hardware ladder/i),
    ).toBeInTheDocument();
  });

  it('surfaces a preview failure inline (role=alert) and raises no toast', async () => {
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([profile()])),
      http.get('*/api/v1/library/files', () =>
        HttpResponse.json([
          { id: 5, filename: 'unit.gcode.3mf', file_type: 'gcode.3mf', file_size: 1 },
        ]),
      ),
      http.get('*/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
      http.post('*/api/v1/eject-profiles/:id/preview', () =>
        HttpResponse.json({ detail: 'generator boom' }, { status: 400 }),
      ),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    // Resolve the model picker, choose a file, and run the preview.
    const modelSelect = await screen.findByLabelText('Printer model');
    await waitFor(() => expect(modelSelect).toHaveValue('H2S'));
    await user.selectOptions(screen.getByLabelText('Library file'), '5');
    await user.click(screen.getByRole('button', { name: /generate preview/i }));

    // The failure renders as a persistent inline alert carrying the API detail.
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('generator boom');

    // ...and ONLY inline: no toast is raised, so the message appears exactly
    // once (a toast would surface the same text a second time).
    expect(screen.getAllByText('generator boom')).toHaveLength(1);
  });
});
