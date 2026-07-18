/**
 * Per-row "Duplicate" action on the Eject Profiles page.
 *
 * Duplicate is the sanctioned preset mechanism: it opens the SAME create/edit
 * dialog in CREATE mode, prefilled from an existing (hardware-validated) profile
 * with a "(copy)" name, so an operator starts a new profile from a real DB row
 * instead of re-typing 11 numeric machine fields. No hardware values are
 * hardcoded — every prefilled value comes from the source row.
 *
 * The source profile below deliberately uses NON-default values for every field
 * so the phantom-assignment guard (test 2) can prove the POST carries the
 * source's real values, not the form defaults or nulls.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EjectProfilesPage } from '../../pages/EjectProfilesPage';

// A source profile whose EVERY field differs from DEFAULT_EJECT_PROFILE_PARAMS,
// so a payload built from defaults would be visibly wrong.
const SOURCE = {
  id: 7,
  name: 'H2C tuned',
  cooldown_temp_c: 33,
  clearance_mm: 12,
  z_offset_mm: 0.6,
  descent_steps: 6,
  x_passes: 9,
  x_margin_mm: 5,
  front_overhang_mm: 4,
  back_overhang_mm: 3,
  eject_speed_mm_min: 4000,
  skim_speed_mm_min: 2000,
  cooling_fan_assist: false,
  final_skim: false,
  max_part_height_mm: 40,
  sweep_x_min_mm: 20,
  sweep_x_max_mm: 300,
  sweep_start_frac: 0.5,
  bed_drop_clearance_mm: 25,
  created_at: '2026-07-01T10:00:00Z',
  updated_at: '2026-07-01T10:00:00Z',
};

// The exact param set a fresh duplicate must POST: the source values verbatim,
// with the "(copy)" name. This is the phantom-assignment contract.
const EXPECTED_COPY_PAYLOAD = {
  name: 'H2C tuned (copy)',
  cooldown_temp_c: 33,
  clearance_mm: 12,
  z_offset_mm: 0.6,
  descent_steps: 6,
  x_passes: 9,
  x_margin_mm: 5,
  front_overhang_mm: 4,
  back_overhang_mm: 3,
  eject_speed_mm_min: 4000,
  skim_speed_mm_min: 2000,
  cooling_fan_assist: false,
  final_skim: false,
  max_part_height_mm: 40,
  sweep_x_min_mm: 20,
  sweep_x_max_mm: 300,
  sweep_start_frac: 0.5,
  bed_drop_clearance_mm: 25,
};

function geometryList() {
  return HttpResponse.json({
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
        bedslinger: false,
        notes: null,
        updated_at: '2026-07-01T00:00:00Z',
      },
    ],
    sweep_band_min_width_mm: 10,
  });
}

function primeBase() {
  server.use(
    http.get('*/api/v1/eject-profiles', () => HttpResponse.json([SOURCE])),
    http.get('*/api/v1/model-geometry', () => geometryList()),
    http.get('*/api/v1/settings/', () => HttpResponse.json({ farm_cooldown_warn_floor_c: 30 })),
    http.get('*/api/v1/library/files', () => HttpResponse.json([])),
  );
}

async function clickDuplicate(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('H2C tuned');
  await user.click(screen.getByRole('button', { name: /duplicate h2c tuned/i }));
  return screen.findByRole('dialog');
}

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

describe('EjectProfilesPage duplicate action', () => {
  it('opens the create dialog seeded from the source with a "(copy)" name', async () => {
    primeBase();
    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await clickDuplicate(user);

    // CREATE mode (not edit): the create title shows and the update title does not.
    expect(within(dialog).getByRole('heading', { name: 'Create eject profile' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('heading', { name: 'Edit eject profile' })).not.toBeInTheDocument();

    // Name pre-set to the translated copy pattern.
    expect(within(dialog).getByLabelText('Name')).toHaveValue('H2C tuned (copy)');

    // Spot-check that numeric machine fields are seeded from the source row,
    // not from the form defaults (cooldown default is 28, x_passes default 11).
    expect(within(dialog).getByLabelText('Cooldown temperature (°C)')).toHaveValue(33);
    expect(within(dialog).getByLabelText('X passes')).toHaveValue(9);
  });

  it('POSTs the SOURCE values (phantom-assignment guard) and never PUTs the source', async () => {
    primeBase();
    let posted: Record<string, unknown> | null = null;
    let putCalled = false;
    server.use(
      http.post('*/api/v1/eject-profiles', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...SOURCE, id: 8, name: 'H2C tuned (copy)' }, { status: 201 });
      }),
      // A duplicate must CREATE, never overwrite the source row.
      http.put('*/api/v1/eject-profiles/:id', () => {
        putCalled = true;
        return HttpResponse.json(SOURCE);
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await clickDuplicate(user);

    // Save WITHOUT touching a single field.
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(posted).not.toBeNull());
    // The payload is the source's real machine values + the "(copy)" name —
    // not defaults (cooldown 28 / x_passes 11 / fan on) and not nulls.
    expect(posted).toMatchObject(EXPECTED_COPY_PAYLOAD);
    // And it went through create, not an update of the source.
    expect(putCalled).toBe(false);
  });

  it('surfaces the backend 409 when duplicating without renaming (no silent failure)', async () => {
    primeBase();
    server.use(
      http.post('*/api/v1/eject-profiles', () =>
        HttpResponse.json(
          { detail: 'An eject profile with that name already exists' },
          { status: 409 },
        ),
      ),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await clickDuplicate(user);

    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));

    // The backend's conflict message renders inline in the still-open dialog.
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('An eject profile with that name already exists');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});
