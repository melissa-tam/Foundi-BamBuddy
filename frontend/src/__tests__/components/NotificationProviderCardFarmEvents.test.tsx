/**
 * Tests for the Farm production event toggles added to NotificationProviderCard
 * (Phase 6 / finding F4).
 *
 * Coverage:
 * - The "Farm production" section renders in the expanded settings panel with a
 *   toggle for every farm event (moved plate-not-empty + the 8 Phase-3 events +
 *   the 3 Phase-6 events).
 * - on_plate_not_empty lives ONLY in the farm section (single home — not left in
 *   the Print Lifecycle section).
 * - Toggling a farm event fires an update mutation with the correct field.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import type { NotificationProvider } from '../../api/client';

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

function buildProvider(overrides: Partial<NotificationProvider> = {}): NotificationProvider {
  return {
    id: 1,
    name: 'Test Provider',
    provider_type: 'ntfy',
    enabled: true,
    config: { server: 'https://ntfy.sh', topic: 'bambuddy' },
    on_print_start: false,
    on_print_complete: false,
    on_print_failed: false,
    on_print_stopped: false,
    on_print_progress: false,
    on_print_missing_spool_assignment: false,
    on_printer_offline: false,
    on_printer_error: false,
    on_ai_failure_detection: false,
    on_filament_low: false,
    on_maintenance_due: false,
    on_ams_humidity_high: false,
    on_ams_temperature_high: false,
    on_ams_ht_humidity_high: false,
    on_ams_ht_temperature_high: false,
    on_plate_not_empty: true,
    on_bed_cooled: false,
    on_first_layer_complete: false,
    on_queue_job_added: false,
    on_queue_job_assigned: false,
    on_queue_job_started: false,
    on_queue_job_waiting: false,
    on_queue_job_skipped: false,
    on_queue_job_failed: false,
    on_queue_completed: false,
    on_stock_reorder_alert: false,
    on_stock_break_alert: false,
    on_first_article_pending: true,
    on_first_article_approved: true,
    on_printer_quarantined: true,
    on_run_paused: true,
    on_run_resumed: false,
    on_run_completed: false,
    on_run_aborted: true,
    on_foreign_job_detected: true,
    on_model_mismatch: true,
    on_run_unit_stopped: true,
    on_print_stalled: true,
    on_storage_low: true,
    on_cooldown_escalation: true,
    quiet_hours_enabled: false,
    quiet_hours_start: null,
    quiet_hours_end: null,
    daily_digest_enabled: false,
    daily_digest_time: null,
    printer_id: null,
    last_success: null,
    last_error: null,
    last_error_at: null,
    created_at: '2026-07-11T00:00:00Z',
    updated_at: '2026-07-11T00:00:00Z',
    ...overrides,
  };
}

async function expandFarmSection() {
  const user = userEvent.setup();
  render(<NotificationProviderCard provider={buildProvider()} onEdit={vi.fn()} />);
  await user.click(await screen.findByText(/event settings/i));
  const section = (await screen.findByText('Farm production')).closest('div')!;
  return { user, section };
}

describe('NotificationProviderCard — Farm production section', () => {
  it('renders the Farm production section header when settings are expanded', async () => {
    const user = userEvent.setup();
    render(<NotificationProviderCard provider={buildProvider()} onEdit={vi.fn()} />);

    await user.click(await screen.findByText(/event settings/i));

    expect(await screen.findByText('Farm production')).toBeInTheDocument();
  });

  it('renders a toggle for every farm event in the section', async () => {
    const { section } = await expandFarmSection();

    const labels = [
      'Plate Not Empty',
      'First article pending',
      'First article approved',
      'Run paused',
      'Run resumed',
      'Run completed',
      'Run aborted',
      'Printer quarantined',
      'Foreign job detected',
      'Model mismatch',
      'Unit stopped',
      'Print stalled',
      'USB storage low',
      'Cooldown running long',
    ];
    for (const label of labels) {
      expect(within(section).getByText(label)).toBeInTheDocument();
    }
    // 14 farm toggles: moved plate-not-empty + 8 Phase-3 + 3 Phase-6 + USB storage
    // low + cooldown escalation.
    expect(within(section).getAllByRole('switch')).toHaveLength(14);
  });

  it('renders on_plate_not_empty exactly once (single home in the farm section)', async () => {
    const user = userEvent.setup();
    render(<NotificationProviderCard provider={buildProvider()} onEdit={vi.fn()} />);
    await user.click(await screen.findByText(/event settings/i));

    // Not duplicated in the Print Lifecycle section — the moved toggle appears once.
    expect(screen.getAllByText('Plate Not Empty')).toHaveLength(1);
    const section = (await screen.findByText('Farm production')).closest('div')!;
    expect(within(section).getByText('Plate Not Empty')).toBeInTheDocument();
  });

  it('farm toggles reflect the provider state', async () => {
    const { section } = await expandFarmSection();
    // run_resumed defaults OFF, run_aborted defaults ON in the fixture.
    const resumedRow = within(section).getByText('Run resumed').closest('.flex')!;
    const abortedRow = within(section).getByText('Run aborted').closest('.flex')!;
    expect(within(resumedRow).getByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(within(abortedRow).getByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });

  it('toggling Run aborted sends the correct PATCH payload', async () => {
    let captured: unknown = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(buildProvider({ on_run_aborted: false }));
      }),
    );

    const { user, section } = await expandFarmSection();
    const abortedRow = within(section).getByText('Run aborted').closest('.flex')!;
    await user.click(within(abortedRow).getByRole('switch'));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured).toMatchObject({ on_run_aborted: false });
  });

  it('toggling Plate Not Empty sends the correct PATCH payload', async () => {
    let captured: unknown = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = await request.json();
        return HttpResponse.json(buildProvider({ on_plate_not_empty: false }));
      }),
    );

    const { user, section } = await expandFarmSection();
    const plateRow = within(section).getByText('Plate Not Empty').closest('.flex')!;
    await user.click(within(plateRow).getByRole('switch'));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured).toMatchObject({ on_plate_not_empty: false });
  });
});
