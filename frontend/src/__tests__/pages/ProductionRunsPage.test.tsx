/**
 * Frontend tests for the Production Runs page (farm production, Phase 2).
 *
 * Coverage:
 * - List renders run cards with progress, status badge, and ETA.
 * - Empty state (with SKUs available) shows the start CTA.
 * - Error state shows the load-error message + retry button.
 * - Start-run dialog validation: target_units >= 1 required (blank and 0
 *   both blocked; no POST fires).
 * - Abort requires the confirmation dialog: clicking Abort does not POST,
 *   confirming does.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ProductionRunsPage } from '../../pages/ProductionRunsPage';
import type { ProductionRun } from '../../types/productionRuns';
import type { Sku } from '../../types/skus';

function run(overrides: Partial<ProductionRun> = {}): ProductionRun {
  return {
    id: 1,
    name: 'WID-001 run',
    sku_code: 'WID-001',
    sku_file_id: 11,
    target_units: 10,
    units_planned: 12,
    units_completed: 4,
    units_failed: 0,
    plates_total: 3,
    plates_completed: 1,
    plates_failed: 0,
    plates_pending: 2,
    status: 'active',
    pause_reason: null,
    staged_filament_short: 0,
    staged_other: 0,
    has_blocked_printers: false,
    require_first_article: true,
    first_article_state: null,
    retry_max_per_unit: 1,
    escalate_consecutive_failures: 3,
    first_article_reject_reason: null,
    eject_profile_id: null,
    cooldown_temp_c_override: null,
    target_model: null,
    eta_seconds: 7200,
    printers: [{ id: 1, name: 'H2S-Alpha' }],
    scheduled_start_at: null,
    created_at: '2026-07-02T10:00:00Z',
    ...overrides,
  };
}

/** A future ISO string, N hours out — a run scheduled to start later. */
function futureIso(hours = 3): string {
  return new Date(Date.now() + hours * 3600_000).toISOString();
}

function skuWithFile(overrides: Partial<Sku> = {}): Sku {
  return {
    id: 1,
    code: 'WID-001',
    name: 'Widget',
    part_number: null,
    notes: null,
    default_eject_profile_id: null,
    files: [
      {
        id: 11,
        sku_id: 1,
        library_file_id: 5,
        library_file_name: 'widget.gcode.3mf',
        plate_index: 1,
        units_per_plate: 4,
        nozzle_diameter: 0.4,
        filament_type: 'PETG',
        printer_model: 'H2S',
        max_z_height: 12.5,
      },
    ],
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-01T10:00:00Z',
    ...overrides,
  };
}

// Dialog queries: printers + eject profiles fire on open.
const emptyEjectProfiles = http.get('*/api/v1/eject-profiles', () => HttpResponse.json([]));

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

describe('ProductionRunsPage', () => {
  it('renders the list of runs with progress, status, and ETA', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([run()])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    expect(await screen.findByText('WID-001 run')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('1 / 3 plates')).toBeInTheDocument();
    expect(screen.getByText('4 / 12')).toBeInTheDocument();
    // eta_seconds=7200 renders humanely as "2h 0m".
    expect(screen.getByText('2h 0m')).toBeInTheDocument();
    expect(screen.getByText(/H2S-Alpha/)).toBeInTheDocument();
  });

  it('shows the empty state with a start CTA when there are no runs', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    expect(await screen.findByText('No production runs yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start run/i })).toBeEnabled();
  });

  it('shows the error state with a retry button when the list fails to load', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => new HttpResponse(null, { status: 500 })),
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
    );

    render(<ProductionRunsPage />);

    expect(await screen.findByText(/could not load production runs/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('blocks the start-run submit when target units is blank or below 1', async () => {
    let postCalled = false;
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      emptyEjectProfiles,
      http.post('*/api/v1/production-runs', () => {
        postCalled = true;
        return HttpResponse.json(run(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('No production runs yet');
    await user.click(screen.getByRole('button', { name: /start run/i }));
    const dialog = await screen.findByRole('dialog');

    const target = screen.getByLabelText(/target units/i);

    // Blank target → blocked.
    await user.clear(target);
    await user.click(within(dialog).getByRole('button', { name: /start run/i }));
    expect(await screen.findByText(/at least 1 unit/i)).toBeInTheDocument();
    expect(postCalled).toBe(false);

    // Zero target → still blocked.
    await user.clear(target);
    await user.type(target, '0');
    await user.click(within(dialog).getByRole('button', { name: /start run/i }));
    expect(await screen.findByText(/at least 1 unit/i)).toBeInTheDocument();
    expect(postCalled).toBe(false);
    expect(dialog).toBeInTheDocument();
  });

  it('starts a run with a valid target and shows the over-production hint', async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json(posted ? [run()] : [])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      // Single-model H2S fleet → the derived default model is H2S (the file's
      // slice model), so target_model stays 'H2S' (no longer a hardcode).
      http.get('*/api/v1/printers/', () =>
        HttpResponse.json([{ id: 1, name: 'H2S-Alpha', model: 'H2S' }]),
      ),
      emptyEjectProfiles,
      http.post('*/api/v1/production-runs', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(run(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('No production runs yet');
    await user.click(screen.getByRole('button', { name: /start run/i }));
    const dialog = await screen.findByRole('dialog');

    const target = screen.getByLabelText(/target units/i);
    await user.clear(target);
    await user.type(target, '10');

    // units_per_plate=4 → 3 plates → 12 planned, 2 over target.
    expect(
      await screen.findByText(/will produce 12 units \(3 plates\)/i),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole('button', { name: /start run/i }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      sku_file_id: 11,
      target_units: 10,
      target_model: 'H2S',
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('surfaces a backend 422 rejection inline in the still-open dialog', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      emptyEjectProfiles,
      http.post('*/api/v1/production-runs', () =>
        HttpResponse.json({ detail: 'farm runs must eject' }, { status: 422 }),
      ),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('No production runs yet');
    await user.click(screen.getByRole('button', { name: /start run/i }));
    const dialog = await screen.findByRole('dialog');

    const target = screen.getByLabelText(/target units/i);
    await user.clear(target);
    await user.type(target, '10');
    await user.click(within(dialog).getByRole('button', { name: /start run/i }));

    // The backend detail renders inline (role=alert) inside the open dialog —
    // not just a transient toast — so the failure never dead-ends.
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('farm runs must eject');
    expect(dialog).toBeInTheDocument();
    // Form is re-submittable (button re-enabled after the failed attempt).
    expect(within(dialog).getByRole('button', { name: /start run/i })).toBeEnabled();
  });

  it('gates abort behind a required confirmation dialog', async () => {
    let abortCalled = false;
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([run()])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      http.post('*/api/v1/production-runs/:id/abort', () => {
        abortCalled = true;
        return HttpResponse.json(run({ status: 'cancelled' }));
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('WID-001 run');
    await user.click(screen.getByRole('button', { name: /abort/i }));

    // Confirmation shown; no POST yet.
    expect(await screen.findByText('Abort this run?')).toBeInTheDocument();
    expect(abortCalled).toBe(false);

    // Cancelling the dialog never aborts.
    await user.click(screen.getByRole('button', { name: /cancel/i }));
    await waitFor(() => expect(screen.queryByText('Abort this run?')).not.toBeInTheDocument());
    expect(abortCalled).toBe(false);

    // Confirming does.
    await user.click(screen.getByRole('button', { name: /abort/i }));
    await screen.findByText('Abort this run?');
    // The confirm modal's confirm button carries the same "Abort" label; it is
    // the last matching button in the DOM (modal overlays the page).
    const abortButtons = screen.getAllByRole('button', { name: /^abort$/i });
    await user.click(abortButtons[abortButtons.length - 1]);
    await waitFor(() => expect(abortCalled).toBe(true));
  });

  // -------------------------------------------------------------------------
  // Phase 4.1: run-card hold visibility + detail link
  // -------------------------------------------------------------------------

  it('links the run card title to the detail page and shows hold chips', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([
          run({
            status: 'paused',
            pause_reason: 'no_available_printers',
            has_blocked_printers: true,
          }),
        ]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    const title = await screen.findByRole('link', { name: 'WID-001 run' });
    expect(title).toHaveAttribute('href', '/production-runs/1');
    expect(screen.getByText('No available printers')).toBeInTheDocument();
    expect(screen.getByText('Printer blocked')).toBeInTheDocument();
  });

  it('shows the low-filament staged banner on the run card', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([run({ staged_filament_short: 2 })]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    expect(await screen.findByText(/low filament — swap the spool/i)).toBeInTheDocument();
  });

  it('hides hold chips on healthy runs', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([run()])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    await screen.findByRole('link', { name: 'WID-001 run' });
    expect(screen.queryByText('Printer blocked')).not.toBeInTheDocument();
    expect(screen.queryByText(/low filament/i)).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Phase 4.3i: cooldown override shows the profile default it would replace
  // -------------------------------------------------------------------------

  it("shows the selected eject profile's cooldown default under the override input", async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () =>
        HttpResponse.json([skuWithFile({ default_eject_profile_id: 5 })]),
      ),
      http.get('*/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 5, name: 'PETG default', cooldown_temp_c: 33 }]),
      ),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('No production runs yet');
    await user.click(screen.getByRole('button', { name: /start run/i }));
    await screen.findByRole('dialog');

    // Eject/cooldown moved under the collapsed "Advanced" disclosure (F5).
    await user.click(screen.getByRole('button', { name: /^advanced$/i }));
    // The dialog seeds the SKU default profile → the hint names its default.
    expect(await screen.findByText('Overrides profile default 33°C')).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Phase 5 (F5): H2C-ready model selection + Advanced section
  // -------------------------------------------------------------------------

  const twoModelFleet = http.get('*/api/v1/printers/', () =>
    HttpResponse.json([
      { id: 1, name: 'H2S-Alpha', model: 'H2S' },
      { id: 2, name: 'H2C-Beta', model: 'H2C' },
    ]),
  );

  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByText('No production runs yet');
    await user.click(screen.getByRole('button', { name: /start run/i }));
    return screen.findByRole('dialog');
  }

  it('derives the model options from the fleet (multi-model → select)', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      twoModelFleet,
      emptyEjectProfiles,
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    const modelSelect = await screen.findByRole('combobox', { name: /printer model/i });
    const options = within(modelSelect).getAllByRole('option').map((o) => o.textContent);
    // Models are sorted alphabetically (H2C before H2S).
    expect(options).toEqual(['Any H2C printer', 'Any H2S printer']);
  });

  it('renders static model text when the fleet has a single model', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      http.get('*/api/v1/printers/', () =>
        HttpResponse.json([{ id: 1, name: 'H2S-Alpha', model: 'H2S' }]),
      ),
      emptyEjectProfiles,
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    expect(await screen.findByText('Any H2S printer')).toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: /printer model/i })).not.toBeInTheDocument();
  });

  it('defaults the model to the file slice model, re-defaulting until the user picks', async () => {
    // Two files: plate 1 sliced for H2S, plate 2 for H2C. The default model
    // follows the selected file until the operator changes the select.
    const twoFileSku = skuWithFile({
      files: [
        {
          id: 11, sku_id: 1, library_file_id: 5, library_file_name: 'a.gcode.3mf',
          plate_index: 1, units_per_plate: 4, nozzle_diameter: 0.4, filament_type: 'PETG',
          printer_model: 'H2S', max_z_height: 12.5,
        },
        {
          id: 12, sku_id: 1, library_file_id: 6, library_file_name: 'b.gcode.3mf',
          plate_index: 2, units_per_plate: 4, nozzle_diameter: 0.4, filament_type: 'PETG',
          printer_model: 'H2C', max_z_height: 12.5,
        },
      ],
    });
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([twoFileSku])),
      twoModelFleet,
      emptyEjectProfiles,
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    const modelSelect = (await screen.findByRole('combobox', {
      name: /printer model/i,
    })) as HTMLSelectElement;
    // File 11 (H2S) is auto-seeded → default model H2S.
    expect(modelSelect.value).toBe('H2S');

    // Switch to file 12 (H2C) → model re-defaults to H2C (untouched).
    await user.selectOptions(screen.getByLabelText(/file \/ plate/i), '12');
    await waitFor(() => expect(modelSelect.value).toBe('H2C'));

    // Operator picks H2S manually; switching the file back no longer re-defaults.
    await user.selectOptions(modelSelect, 'H2S');
    await user.selectOptions(screen.getByLabelText(/file \/ plate/i), '11');
    // Give any effect a tick; the model must stay on the operator's choice.
    await new Promise((r) => setTimeout(r, 0));
    expect(modelSelect.value).toBe('H2S');
  });

  it('warns (soft, non-blocking) when the chosen model differs from the file model', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])), // file model H2S
      twoModelFleet,
      emptyEjectProfiles,
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    const modelSelect = await screen.findByRole('combobox', { name: /printer model/i });
    // Matching (H2S) → no warning.
    expect(screen.queryByText(/capability gate will hold/i)).not.toBeInTheDocument();
    // Pick H2C against an H2S file → soft warning appears.
    await user.selectOptions(modelSelect, 'H2C');
    expect(await screen.findByText(/sliced for H2S but targets H2C/i)).toBeInTheDocument();
    // Never a client-side block: submit remains enabled.
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('button', { name: /start run/i })).toBeEnabled();
  });

  it('warns in specific-printers mode when a checked printer model differs', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])), // file model H2S
      twoModelFleet,
      emptyEjectProfiles,
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    await user.click(screen.getByRole('radio', { name: /specific printers/i }));
    // Check the H2C printer against the H2S file → warning.
    await user.click(await screen.findByRole('checkbox', { name: /H2C-Beta/i }));
    expect(await screen.findByText(/sliced for H2S but targets H2C/i)).toBeInTheDocument();
  });

  it('collapses the Advanced section by default and toggles aria-expanded', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      twoModelFleet,
      emptyEjectProfiles,
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    const toggle = await screen.findByRole('button', { name: /^advanced$/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    // Collapsed → the eject-profile field is not rendered.
    expect(screen.queryByLabelText(/eject profile/i)).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText(/eject profile/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/retries per plate/i)).toBeInTheDocument();
  });

  it('surfaces a non-default advanced value in the collapsed summary line', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      twoModelFleet,
      http.get('*/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 5, name: 'PETG default', cooldown_temp_c: 33 }]),
      ),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    await openDialog(user);

    // Set a cooldown override, then collapse — the summary must name it so an
    // override is never invisible.
    await user.click(await screen.findByRole('button', { name: /^advanced$/i }));
    await user.type(screen.getByLabelText(/cooldown override/i), '34');
    await user.click(screen.getByRole('button', { name: /^advanced$/i }));
    expect(await screen.findByText(/cooldown override.*34/i)).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Phase 5 (F9): Run again
  // -------------------------------------------------------------------------

  it('shows Run again on terminal runs only (permission-gated)', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([
          run({ id: 1, name: 'Active run', status: 'active' }),
          run({ id: 2, name: 'Done run', status: 'completed' }),
        ]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    await screen.findByText('Done run');
    // Exactly one Run again button — the completed run's; the active run has none.
    expect(screen.getAllByRole('button', { name: /run again/i })).toHaveLength(1);
  });

  it('reopens the dialog prefilled from a finished run and repeats its shape', async () => {
    let posted: Record<string, unknown> | null = null;
    const finished = run({
      id: 7,
      status: 'completed',
      sku_file_id: 11,
      target_units: 10,
      target_model: 'H2C',
      eject_profile_id: 5,
      cooldown_temp_c_override: 34,
      require_first_article: true,
      retry_max_per_unit: 2,
      escalate_consecutive_failures: 4,
    });
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([finished])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      twoModelFleet,
      http.get('*/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 5, name: 'PETG default', cooldown_temp_c: 33 }]),
      ),
      http.post('*/api/v1/production-runs', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(finished, { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('WID-001 run');
    await user.click(screen.getByRole('button', { name: /run again/i }));
    const dialog = await screen.findByRole('dialog');

    // Prefilled from the run: target units + model (both strategies covered by
    // the two model tests above — this run is model-targeted).
    expect((within(dialog).getByLabelText(/target units/i) as HTMLInputElement).value).toBe('10');
    const modelSelect = within(dialog).getByRole('combobox', {
      name: /printer model/i,
    }) as HTMLSelectElement;
    expect(modelSelect.value).toBe('H2C');

    // The eject profile + cooldown override are prefilled inside Advanced.
    // Collapsed, the toggle's name also carries the non-default summary, so match
    // by prefix rather than an exact "Advanced".
    await user.click(within(dialog).getByRole('button', { name: /^advanced/i }));
    expect((within(dialog).getByLabelText(/eject profile/i) as HTMLSelectElement).value).toBe('5');
    expect((within(dialog).getByLabelText(/cooldown override/i) as HTMLInputElement).value).toBe('34');

    await user.click(within(dialog).getByRole('button', { name: /start run/i }));
    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({
      sku_file_id: 11,
      target_units: 10,
      target_model: 'H2C',
      eject_profile_id: 5,
      cooldown_temp_c_override: 34,
      require_first_article: true,
      retry_max_per_unit: 2,
      escalate_consecutive_failures: 4,
    });
    expect(posted).not.toHaveProperty('printer_ids');
  });

  // -------------------------------------------------------------------------
  // Phase 5: scheduled runs (one-time deferred start)
  // -------------------------------------------------------------------------

  it('shows the Scheduled badge and Start-now / Reschedule actions on a scheduled run', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([run({ scheduled_start_at: futureIso(3) })]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    await screen.findByText('WID-001 run');
    expect(screen.getByText('Scheduled')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /start now/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reschedule/i })).toBeInTheDocument();
    // A scheduled run offers Start-now instead of Pause (it hasn't started).
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it('Start now posts a reschedule with a null start time', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([run({ scheduled_start_at: futureIso(3) })]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      http.post('*/api/v1/production-runs/:id/reschedule', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(run());
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('WID-001 run');
    await user.click(screen.getByRole('button', { name: /start now/i }));
    await waitFor(() => expect(body).not.toBeNull());
    expect(body).toEqual({ scheduled_start_at: null });
  });

  it('a fresh (ASAP) run posts a null scheduled_start_at', async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      twoModelFleet,
      emptyEjectProfiles,
      http.post('*/api/v1/production-runs', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(run(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    const dialog = await openDialog(user);
    const target = screen.getByLabelText(/target units/i);
    await user.clear(target);
    await user.type(target, '10');
    await user.click(within(dialog).getByRole('button', { name: /start run/i }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(posted).toMatchObject({ scheduled_start_at: null });
  });

  it('scheduling for later posts a future scheduled_start_at', async () => {
    let posted: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/production-runs', () => HttpResponse.json([])),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      twoModelFleet,
      emptyEjectProfiles,
      http.post('*/api/v1/production-runs', async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(run(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);
    const dialog = await openDialog(user);
    const target = screen.getByLabelText(/target units/i);
    await user.clear(target);
    await user.type(target, '10');
    // Switch to "Schedule for later" → the field seeds now+1h and emits an ISO.
    await user.click(within(dialog).getByRole('button', { name: /schedule for later/i }));
    await user.click(within(dialog).getByRole('button', { name: /start run/i }));

    await waitFor(() => expect(posted).not.toBeNull());
    expect(typeof posted!.scheduled_start_at).toBe('string');
    expect(new Date(posted!.scheduled_start_at as string).getTime()).toBeGreaterThan(Date.now());
  });
});
