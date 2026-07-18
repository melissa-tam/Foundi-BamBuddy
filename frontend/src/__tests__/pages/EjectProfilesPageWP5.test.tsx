/**
 * WP5 additions to the Eject Profiles page:
 *  - Dry-run dispatch: the "allow unvalidated geometry" override checkbox is
 *    offered ONLY when the selected printer's model is a registry row whose
 *    hardware ladder has not been passed, and the dispatch body carries
 *    allow_unvalidated: true only when that checkbox is ticked.
 *  - Model geometry registry manager: renders one row per registry model
 *    (em-dash + tooltip for a null Z travel, motion badge from `bedslinger`),
 *    hides the edit affordance without eject_profiles:update, and routes a
 *    `validated` flip through a confirm step before the PUT fires.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { EjectProfilesPage } from '../../pages/EjectProfilesPage';

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

function geoRow(overrides: Partial<Record<string, unknown>> = {}) {
  return {
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
    ...overrides,
  };
}

function geometryList(rows: ReturnType<typeof geoRow>[]) {
  return HttpResponse.json({ geometries: rows, sweep_band_min_width_mm: 10 });
}

const printers = [
  { id: 1, name: 'P-H2S', model: 'H2S', is_active: true, serial_number: 's1', ip_address: '1', nozzle_count: 1, location: null, auto_archive: true, created_at: '', updated_at: '' },
  { id: 2, name: 'P-H2C', model: 'H2C', is_active: true, serial_number: 's2', ip_address: '2', nozzle_count: 2, location: null, auto_archive: true, created_at: '', updated_at: '' },
];

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Dry-run: unvalidated-geometry override checkbox
// ---------------------------------------------------------------------------

describe('EjectProfilesPage dry-run allow_unvalidated', () => {
  function primeDryRun() {
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([profile()])),
      http.get('*/api/v1/model-geometry', () =>
        geometryList([geoRow(), geoRow({ model_key: 'H2C', validated: false })]),
      ),
      http.get('*/api/v1/settings/', () => HttpResponse.json({ farm_cooldown_warn_floor_c: 30 })),
      http.get('*/api/v1/printers/', () => HttpResponse.json(printers)),
      http.get('*/api/v1/library/files', () =>
        HttpResponse.json([{ id: 5, filename: 'unit.gcode.3mf', file_type: 'gcode.3mf', file_size: 1 }]),
      ),
      http.get('*/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
    );
  }

  async function openDryRun(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByText('Fast sweep');
    await user.click(screen.getByRole('button', { name: /dry run fast sweep/i }));
    return screen.findByRole('dialog');
  }

  it('hides the override when the selected printer model is hardware-validated', async () => {
    primeDryRun();
    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await openDryRun(user);

    // Wait for the connected-printer list, then pick the validated H2S printer.
    await within(dialog).findByRole('option', { name: /P-H2S/i });
    await user.selectOptions(within(dialog).getByLabelText('Printer'), '1');

    expect(
      within(dialog).queryByRole('checkbox', { name: /allow unvalidated geometry/i }),
    ).not.toBeInTheDocument();
  });

  it('moves focus into the dry-run dialog on open and closes it on Escape (shared Modal)', async () => {
    primeDryRun();
    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await openDryRun(user);

    // Modal moves focus into the panel on open (WCAG dialog focus management).
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    // Escape dismisses the dialog (not blocked because no dispatch is in flight).
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('offers the override for an unvalidated model and sends allow_unvalidated only when ticked', async () => {
    primeDryRun();
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.post('*/api/v1/eject-profiles/:id/dry-run/dispatch', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ queue_item_id: 99, library_file_id: 5, message: 'queued' });
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await openDryRun(user);

    await within(dialog).findByRole('option', { name: /P-H2C/i });
    await user.selectOptions(within(dialog).getByLabelText('Printer'), '2');
    await user.selectOptions(within(dialog).getByLabelText('Library file'), '5');

    const checkbox = await within(dialog).findByRole('checkbox', {
      name: /allow unvalidated geometry/i,
    });

    // First dispatch WITHOUT ticking: the override key is absent.
    await user.click(within(dialog).getByRole('button', { name: /dispatch test/i }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).not.toHaveProperty('allow_unvalidated');

    // Tick the override and dispatch again: allow_unvalidated: true is sent.
    posted = null;
    await user.click(checkbox);
    // The amber override warning appears once it is armed.
    expect(within(dialog).getByText(/has NOT been hardware-validated/i)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: /dispatch test/i }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ printer_id: 2, library_file_id: 5, allow_unvalidated: true });
  });
});

// ---------------------------------------------------------------------------
// Dry-run: sole-connected-printer auto-select
// ---------------------------------------------------------------------------

describe('EjectProfilesPage dry-run printer auto-select', () => {
  function primeDryRunWith(printerList: typeof printers) {
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([profile()])),
      http.get('*/api/v1/model-geometry', () =>
        geometryList([geoRow(), geoRow({ model_key: 'H2C', validated: false })]),
      ),
      http.get('*/api/v1/settings/', () => HttpResponse.json({ farm_cooldown_warn_floor_c: 30 })),
      http.get('*/api/v1/printers/', () => HttpResponse.json(printerList)),
      http.get('*/api/v1/library/files', () =>
        HttpResponse.json([{ id: 5, filename: 'unit.gcode.3mf', file_type: 'gcode.3mf', file_size: 1 }]),
      ),
      http.get('*/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({ plates: [{ index: 1, name: 'Plate 1' }] }),
      ),
    );
  }

  async function openDryRun(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByText('Fast sweep');
    await user.click(screen.getByRole('button', { name: /dry run fast sweep/i }));
    return screen.findByRole('dialog');
  }

  it('preselects the sole connected printer with the override left unticked', async () => {
    // Only the unvalidated H2C printer is active (default status ⇒ connected),
    // so exactly one connected printer exists.
    primeDryRunWith([printers[1]]);
    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await openDryRun(user);

    // Once the connectivity probe settles, the lone printer is auto-selected.
    const select = within(dialog).getByLabelText('Printer');
    await waitFor(() => expect(select).toHaveValue('2'));

    // Its model is unvalidated, so the override checkbox is shown — and it must
    // start UNCHECKED: auto-selecting a printer never ticks the override.
    const checkbox = within(dialog).getByRole('checkbox', {
      name: /allow unvalidated geometry/i,
    });
    expect(checkbox).not.toBeChecked();
  });

  it('keeps the placeholder when more than one printer is connected', async () => {
    primeDryRunWith(printers); // both P-H2S and P-H2C connect
    const user = userEvent.setup();
    render(<EjectProfilesPage />);
    const dialog = await openDryRun(user);

    // Wait until BOTH connected printers are listed (all probes settled) so the
    // assertion cannot pass on a transient mid-load state.
    await within(dialog).findByRole('option', { name: /P-H2S/i });
    await within(dialog).findByRole('option', { name: /P-H2C/i });

    // Multiple connected printers ⇒ nothing is auto-selected.
    expect(within(dialog).getByLabelText('Printer')).toHaveValue('');
    // No printer chosen ⇒ the unvalidated override checkbox is not shown.
    expect(
      within(dialog).queryByRole('checkbox', { name: /allow unvalidated geometry/i }),
    ).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Model geometry registry manager
// ---------------------------------------------------------------------------

describe('EjectProfilesPage geometry manager', () => {
  it('renders a row per model with a null-Z em-dash tooltip and the bedslinger badge', async () => {
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.get('*/api/v1/settings/', () => HttpResponse.json({ farm_cooldown_warn_floor_c: 30 })),
      http.get('*/api/v1/model-geometry', () =>
        geometryList([
          geoRow(),
          geoRow({ model_key: 'A1', bed_x: 256, bed_y: 256, z_travel_mm: null, bedslinger: true }),
        ]),
      ),
    );

    render(<EjectProfilesPage />);

    // Both rows render.
    expect(await screen.findByText('A1')).toBeInTheDocument();
    expect(screen.getByText('H2S')).toBeInTheDocument();
    // A1 is a bedslinger; H2S is bed-on-Z.
    expect(screen.getByText('Bedslinger')).toBeInTheDocument();
    expect(screen.getByText('Bed on Z')).toBeInTheDocument();
    // A1 has no Z travel → em-dash carrying the "no Z travel" accessible name.
    expect(screen.getByLabelText(/no z travel registered/i)).toBeInTheDocument();
  });

  it('hides the edit affordance without eject_profiles:update', async () => {
    // Auth enabled + no token ⇒ user is null ⇒ no permissions ⇒ not admin.
    server.use(
      http.get('*/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: true, requires_setup: false }),
      ),
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.get('*/api/v1/settings/', () => HttpResponse.json({ farm_cooldown_warn_floor_c: 30 })),
      http.get('*/api/v1/model-geometry', () => geometryList([geoRow()])),
    );

    render(<EjectProfilesPage />);

    await screen.findByText('H2S');
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: /edit geometry h2s/i }),
      ).not.toBeInTheDocument(),
    );
  });

  it('routes a validated flip through a confirm step before the PUT fires', async () => {
    let putCalled = false;
    let putBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/eject-profiles', () => HttpResponse.json([])),
      http.get('*/api/v1/settings/', () => HttpResponse.json({ farm_cooldown_warn_floor_c: 30 })),
      http.get('*/api/v1/model-geometry', () =>
        geometryList([geoRow({ model_key: 'H2C', validated: false, z_travel_mm: 325 })]),
      ),
      http.put('*/api/v1/model-geometry/:modelKey', async ({ request }) => {
        putCalled = true;
        putBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(geoRow({ model_key: 'H2C', validated: true, z_travel_mm: 325 }));
      }),
    );

    const user = userEvent.setup();
    render(<EjectProfilesPage />);

    await screen.findByText('H2C');
    await user.click(screen.getByRole('button', { name: /edit geometry h2c/i }));

    const dialog = await screen.findByRole('dialog');
    // Flip the validated toggle (false → true) and hit save.
    await user.click(within(dialog).getByRole('switch', { name: 'Hardware-validated' }));
    await user.click(within(dialog).getByRole('button', { name: /^save$/i }));

    // A confirm step appears; the PUT has NOT fired yet.
    expect(await screen.findByText('Mark model validated?')).toBeInTheDocument();
    expect(putCalled).toBe(false);

    // Confirming fires the PUT with only the changed field.
    await user.click(screen.getByRole('button', { name: /^confirm$/i }));
    await waitFor(() => expect(putCalled).toBe(true));
    expect(putBody).toEqual({ validated: true });
  });
});
