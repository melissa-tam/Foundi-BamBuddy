/**
 * Run detail page (Phase 4.1): loading / error / not-found / populated states,
 * hold chips (pause reason + blocked printers), the staged banner, per-printer
 * blocked-state chips with reasons, and the unit table (stop attribution,
 * waiting-reason copy, retry lineage, error messages).
 */

import { describe, it, expect, afterEach } from 'vitest';
import { Route, Routes } from 'react-router-dom';
import { screen, within } from '@testing-library/react';
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
    ...overrides,
  };
}

function unit(overrides: Partial<RunUnit> = {}): RunUnit {
  return {
    id: 101,
    status: 'completed',
    stop_source: null,
    waiting_reason: null,
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
    created_at: '2026-07-06T09:00:00Z',
    ...overrides,
  };
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
    // Per-printer reasons
    expect(screen.getByText('Quarantined')).toBeInTheDocument();
    expect(screen.getByText(/device reports H2C, registered as H2S/)).toBeInTheDocument();
    expect(screen.getByText('Offline-stalled mid-print')).toBeInTheDocument();
    expect(screen.getByText('Plate not empty (printer vision)')).toBeInTheDocument();
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
});
