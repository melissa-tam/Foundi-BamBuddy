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
    require_first_article: true,
    first_article_state: null,
    retry_max_per_unit: 1,
    escalate_consecutive_failures: 3,
    first_article_reject_reason: null,
    eta_seconds: 7200,
    printers: [{ id: 1, name: 'H2S-Alpha' }],
    created_at: '2026-07-02T10:00:00Z',
    ...overrides,
  };
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

  it('renders the first-article banner with three actions when awaiting approval', async () => {
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([run({ first_article_state: 'awaiting_approval' })]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
    );

    render(<ProductionRunsPage />);

    expect(await screen.findByText('First article awaiting approval')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /I removed the part/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /eject remotely/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Reject/i })).toBeInTheDocument();
  });

  it('approves the first article (physical) with eject_remotely=false after confirming', async () => {
    let approveBody: Record<string, unknown> | null = null;
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([run({ first_article_state: 'awaiting_approval' })]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      http.post('*/api/v1/production-runs/:id/first-article/approve', async ({ request }) => {
        approveBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(run({ first_article_state: 'approved' }));
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('First article awaiting approval');
    await user.click(screen.getByRole('button', { name: /I removed the part/i }));

    // Confirmation shown; no POST until confirmed.
    expect(await screen.findByText('Approve first article?')).toBeInTheDocument();
    expect(approveBody).toBeNull();

    await user.click(screen.getByRole('button', { name: /^Approve$/ }));

    await waitFor(() => expect(approveBody).not.toBeNull());
    expect(approveBody).toEqual({ eject_remotely: false });
  });

  it('requires a reason to reject and surfaces a 409 inline in the reject dialog', async () => {
    let rejectCalls = 0;
    server.use(
      http.get('*/api/v1/production-runs', () =>
        HttpResponse.json([run({ first_article_state: 'awaiting_approval' })]),
      ),
      http.get('*/api/v1/skus', () => HttpResponse.json([skuWithFile()])),
      http.post('*/api/v1/production-runs/:id/first-article/reject', () => {
        rejectCalls += 1;
        return HttpResponse.json({ detail: 'run is not awaiting approval' }, { status: 409 });
      }),
    );

    const user = userEvent.setup();
    render(<ProductionRunsPage />);

    await screen.findByText('First article awaiting approval');
    await user.click(screen.getByRole('button', { name: /^Reject/i }));

    const dialog = await screen.findByRole('dialog');

    // Empty reason → validation error, no POST.
    await user.click(within(dialog).getByRole('button', { name: /reject first article/i }));
    expect(await within(dialog).findByText(/a reason is required/i)).toBeInTheDocument();
    expect(rejectCalls).toBe(0);

    // With a reason → the 409 detail renders inline (role=alert) in the still-open dialog.
    await user.type(within(dialog).getByLabelText(/^reason$/i), 'warped corners');
    await user.click(within(dialog).getByRole('button', { name: /reject first article/i }));

    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('run is not awaiting approval');
    expect(rejectCalls).toBe(1);
    expect(dialog).toBeInTheDocument();
  });
});
