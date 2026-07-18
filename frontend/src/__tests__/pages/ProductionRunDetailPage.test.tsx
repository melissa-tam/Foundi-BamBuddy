/**
 * Run detail page (Phase 4.1): loading / error / not-found / populated states,
 * hold chips (pause reason + blocked printers), the staged banner, per-printer
 * blocked-state chips with reasons, and the unit table (stop attribution,
 * waiting-reason copy, retry lineage, error messages).
 */

import { describe, it, expect, afterEach } from 'vitest';
import { Route, Routes } from 'react-router-dom';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ProductionRunDetailPage } from '../../pages/ProductionRunDetailPage';
import type { ProductionRun, RunPrinterState, RunUnit } from '../../types/productionRuns';

function printerState(overrides: Partial<RunPrinterState> = {}): RunPrinterState {
  return {
    printer_id: 1,
    name: 'H2S-Alpha',
    connected: true,
    quarantined: false,
    awaiting_plate_clear: false,
    model_mismatch: false,
    model_mismatch_reason: null,
    stalled: false,
    vision_hold: false,
    filament_short_live: false,
    filament_short_detail: null,
    no_usb_drive: false,
    capability_reason: null,
    ...overrides,
  };
}

function unit(overrides: Partial<RunUnit> = {}): RunUnit {
  return {
    id: 101,
    status: 'completed',
    stop_source: null,
    waiting_reason: null,
    scheduled_time: null,
    printer_id: 1,
    printer_name: 'H2S-Alpha',
    started_at: '2026-07-06T10:00:00Z',
    completed_at: '2026-07-06T12:00:00Z',
    retry_of_id: null,
    retry_count: 0,
    filament_short: false,
    manual_start: false,
    first_article: false,
    error_message: null,
    ...overrides,
  };
}

function detailRun(overrides: Partial<ProductionRun> = {}): ProductionRun {
  return {
    id: 1,
    name: 'WID-001 run',
    sku_code: 'WID-001',
    sku_file_id: 11,
    target_units: 4,
    units_planned: 4,
    units_completed: 1,
    units_failed: 0,
    plates_total: 4,
    plates_completed: 1,
    plates_failed: 0,
    plates_pending: 3,
    status: 'active',
    pause_reason: null,
    staged_filament_short: 0,
    staged_other: 0,
    has_blocked_printers: false,
    printer_states: [printerState()],
    units: [unit()],
    require_first_article: false,
    first_article_state: null,
    first_article_reject_reason: null,
    retry_max_per_unit: 1,
    escalate_consecutive_failures: 2,
    eta_seconds: null,
    printers: [{ id: 1, name: 'H2S-Alpha' }],
    scheduled_start_at: null,
    created_at: '2026-07-06T09:00:00Z',
    ...overrides,
  };
}

/** A future ISO string, N hours out — a run scheduled to start later. */
function futureIso(hours = 3): string {
  return new Date(Date.now() + hours * 3600_000).toISOString();
}

function renderDetail(runId = 1) {
  window.history.pushState({}, '', `/production-runs/${runId}`);
  return render(
    <Routes>
      <Route path="/production-runs/:id" element={<ProductionRunDetailPage />} />
    </Routes>,
  );
}

const printerStatusHandler = http.get('*/api/v1/printers/:id/status', () =>
  HttpResponse.json({ connected: true, state: 'IDLE', temperatures: { bed: 31 } }),
);

afterEach(() => {
  server.resetHandlers();
});

describe('ProductionRunDetailPage', () => {
  it('shows the loading state, then the populated header and progress', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () => HttpResponse.json(detailRun())),
      printerStatusHandler,
    );

    renderDetail();
    expect(screen.getByRole('status')).toBeInTheDocument(); // loading spinner

    expect(await screen.findByText('WID-001 run')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('1 / 4 plates')).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /all production runs/i })).toBeInTheDocument();
  });

  it('shows the error state with retry when the fetch fails', async () => {
    server.use(http.get('*/api/v1/production-runs/1', () => new HttpResponse(null, { status: 500 })));

    renderDetail();

    expect(await screen.findByText(/could not load this production run/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('shows the not-found copy for a 404', async () => {
    server.use(
      http.get('*/api/v1/production-runs/999', () =>
        HttpResponse.json({ detail: 'Production run not found' }, { status: 404 }),
      ),
    );

    renderDetail(999);

    expect(await screen.findByText('Production run not found.')).toBeInTheDocument();
  });

  it('renders hold chips, the staged banner and per-printer blocked reasons', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () =>
        HttpResponse.json(
          detailRun({
            status: 'active',
            pause_reason: 'operator_stop',
            has_blocked_printers: true,
            staged_filament_short: 2,
            staged_other: 1,
            printer_states: [
              printerState({ quarantined: true }),
              printerState({
                printer_id: 2,
                name: 'H2S-Beta',
                model_mismatch: true,
                model_mismatch_reason: 'device reports H2C, registered as H2S',
                stalled: true,
                vision_hold: true,
              }),
            ],
          }),
        ),
      ),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    // Hold chips
    expect(screen.getByText(/unit stopped by operator/i)).toBeInTheDocument();
    expect(screen.getByText('Printer blocked')).toBeInTheDocument();
    // Staged banners (low-spool actionable + generic)
    expect(screen.getByText(/low filament — swap the spool/i)).toBeInTheDocument();
    expect(screen.getByText(/1 unit\(s\) staged — press Resume/i)).toBeInTheDocument();
    // Per-printer reasons (now appear in both the chip and the not-eligible
    // panel, so the quarantine label + mismatch reason are non-unique).
    expect(screen.getAllByText('Quarantined').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/device reports H2C, registered as H2S/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Offline-stalled mid-print')).toBeInTheDocument();
    expect(screen.getByText('Plate not empty (printer vision)')).toBeInTheDocument();
    // Not-eligible panel lists the two blocked printers up front.
    expect(screen.getByText('Printers not participating')).toBeInTheDocument();
  });

  it('lists live filament / USB / capability blocks in the not-eligible panel and chips', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () =>
        HttpResponse.json(
          detailRun({
            printer_states: [
              printerState({
                filament_short_live: true,
                filament_short_detail: 'needs 455 g, 260 g on spool',
              }),
              printerState({
                printer_id: 2,
                name: 'H2S-Beta',
                no_usb_drive: true,
                capability_reason: 'Nozzle 0.4 != required 0.6',
              }),
            ],
          }),
        ),
      ),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    expect(screen.getByText('Printers not participating')).toBeInTheDocument();
    // Live filament detail renders verbatim (backend-authored grams sentence).
    expect(screen.getAllByText(/needs 455 g, 260 g on spool/).length).toBeGreaterThanOrEqual(1);
    // No-USB label + capability sentence surface as blocked reasons.
    expect(screen.getAllByText('No USB drive').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Nozzle 0.4 != required 0.6').length).toBeGreaterThanOrEqual(1);
  });

  it('renders no not-eligible panel when every targeted printer is eligible', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () => HttpResponse.json(detailRun())),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    expect(screen.queryByText('Printers not participating')).not.toBeInTheDocument();
  });

  it('renders the unit table with stop attribution, waiting copy and retry lineage', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () =>
        HttpResponse.json(
          detailRun({
            units: [
              unit({ id: 101, status: 'failed', error_message: 'HMS 0300_8017' }),
              unit({
                id: 102,
                status: 'pending',
                retry_of_id: 101,
                retry_count: 1,
                started_at: null,
                completed_at: null,
              }),
              unit({
                id: 103,
                status: 'cancelled',
                stop_source: 'operator_screen',
              }),
              unit({
                id: 104,
                status: 'printing',
                waiting_reason: 'plate_not_empty_printer_detected',
                completed_at: null,
              }),
            ],
          }),
        ),
      ),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    const table = screen.getByRole('table');
    expect(within(table).getByText('Stopped by operator')).toBeInTheDocument();
    expect(within(table).getByText('Retry #1 of unit 101')).toBeInTheDocument();
    expect(within(table).getByText('HMS 0300_8017')).toBeInTheDocument();
    expect(
      within(table).getByText(/printer vision: plate not empty/i),
    ).toBeInTheDocument();
    // Unit ids render for cross-referencing.
    expect(within(table).getByText('#101')).toBeInTheDocument();
    expect(within(table).getByText('#104')).toBeInTheDocument();
  });

  it('renders the first-article banner in the header when awaiting approval (Phase 4, F1)', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () =>
        HttpResponse.json(
          detailRun({
            require_first_article: true,
            first_article_state: 'awaiting_approval',
            first_article_photo_url: '/api/v1/archives/9/photos/finish_1.jpg',
            first_article_printer_id: 1,
            first_article_printer_name: 'H2S-Alpha',
          }),
        ),
      ),
      printerStatusHandler,
    );

    renderDetail();

    // The self-contained approval banner (not just the per-unit badge) now
    // lives on the detail page: its heading, actions, and finish photo.
    expect(await screen.findByText('First article awaiting approval')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /I removed the part/i })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /first article finish photo/i })).toBeInTheDocument();
    // Camera stays collapsed until requested.
    expect(screen.getByRole('button', { name: /view camera/i })).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Header lifecycle controls (Pause / Abort / Resume / Start-now / Reschedule)
  // -------------------------------------------------------------------------

  it('shows Pause and Abort on an active run (not Resume)', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () => HttpResponse.json(detailRun({ status: 'active' }))),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    expect(screen.getByRole('button', { name: /^pause$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^abort$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^resume$/i })).not.toBeInTheDocument();
  });

  it('shows Resume (and Abort) on a paused run, not Pause', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () => HttpResponse.json(detailRun({ status: 'paused' }))),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    expect(screen.getByRole('button', { name: /^resume$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^abort$/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });

  it('gates abort behind a confirmation dialog, then posts', async () => {
    let abortCalled = false;
    server.use(
      http.get('*/api/v1/production-runs/1', () => HttpResponse.json(detailRun({ status: 'active' }))),
      printerStatusHandler,
      http.post('*/api/v1/production-runs/1/abort', () => {
        abortCalled = true;
        return HttpResponse.json(detailRun({ status: 'cancelled' }));
      }),
    );

    const user = userEvent.setup();
    renderDetail();
    await screen.findByText('WID-001 run');

    await user.click(screen.getByRole('button', { name: /^abort$/i }));
    // Confirmation shown; nothing posted yet.
    expect(await screen.findByText('Abort this run?')).toBeInTheDocument();
    expect(abortCalled).toBe(false);

    // Confirm — the modal's confirm button carries the same "Abort" label and
    // is the last matching button in the DOM (the dialog overlays the header).
    const abortButtons = screen.getAllByRole('button', { name: /^abort$/i });
    await user.click(abortButtons[abortButtons.length - 1]);
    await waitFor(() => expect(abortCalled).toBe(true));
  });

  it('shows Start-now and Reschedule on a scheduled run, not Pause', async () => {
    server.use(
      http.get('*/api/v1/production-runs/1', () =>
        HttpResponse.json(detailRun({ status: 'active', scheduled_start_at: futureIso(3) })),
      ),
      printerStatusHandler,
    );

    renderDetail();
    await screen.findByText('WID-001 run');

    expect(screen.getByRole('button', { name: /start now/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reschedule/i })).toBeInTheDocument();
    // A scheduled run hasn't started — Pause is not offered.
    expect(screen.queryByRole('button', { name: /^pause$/i })).not.toBeInTheDocument();
  });
});
