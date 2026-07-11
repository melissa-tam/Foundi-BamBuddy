/**
 * FirstArticleBanner (farm production; self-contained in Phase 4, F1).
 *
 * Covers the extracted banner in isolation on the run it's handed:
 * - the finish photo renders (tap-to-open link) and falls back to a
 *   "photo unavailable" note when the image errors (403 / pruned);
 * - the camera is collapsed by default and mounts CameraTile (mocked) on
 *   demand, then hides again;
 * - the approve (physical) and reject mutation flows still fire, including the
 *   inline 409 alert in the still-open reject dialog.
 *
 * (These approve/reject cases were moved here from ProductionRunsPage.test.tsx
 * when the banner was extracted.)
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import type { ProductionRun } from '../../types/productionRuns';

// The banner mounts CameraTile for the FA printer; stub it so the test asserts
// the mount/collapse behaviour without a real MJPEG/stream-token pull.
vi.mock('../../components/CameraTile', () => ({
  CameraTile: ({ printerId }: { printerId: number }) => (
    <div data-testid="camera-tile">camera {printerId}</div>
  ),
}));

import { FirstArticleBanner } from '../../components/FirstArticleBanner';

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
    first_article_state: 'awaiting_approval',
    retry_max_per_unit: 1,
    escalate_consecutive_failures: 3,
    first_article_reject_reason: null,
    first_article_photo_url: null,
    first_article_printer_id: null,
    first_article_printer_name: null,
    eject_profile_id: null,
    cooldown_temp_c_override: null,
    target_model: null,
    eta_seconds: 7200,
    printers: [{ id: 1, name: 'H2S-Alpha' }],
    created_at: '2026-07-02T10:00:00Z',
    ...overrides,
  };
}

afterEach(() => server.resetHandlers());

describe('FirstArticleBanner', () => {
  it('renders the finish photo and three actions when awaiting approval', async () => {
    render(
      <FirstArticleBanner
        run={run({
          first_article_photo_url: '/api/v1/archives/9/photos/finish_1.jpg',
          first_article_printer_id: 3,
          first_article_printer_name: 'H2S-Alpha',
        })}
      />,
    );

    expect(await screen.findByText('First article awaiting approval')).toBeInTheDocument();
    const img = screen.getByRole('img', { name: /first article finish photo/i });
    expect(img).toHaveAttribute('src', expect.stringContaining('/photos/finish_1.jpg'));
    // Tap opens the full-size photo in a new tab.
    expect(img.closest('a')).toHaveAttribute('target', '_blank');

    expect(screen.getByRole('button', { name: /I removed the part/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /eject remotely/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Reject/i })).toBeInTheDocument();
  });

  it('swaps to the fallback note when the finish photo fails to load', async () => {
    render(
      <FirstArticleBanner
        run={run({ first_article_photo_url: '/api/v1/archives/9/photos/finish_1.jpg', first_article_printer_id: 3 })}
      />,
    );

    const img = await screen.findByRole('img', { name: /first article finish photo/i });
    fireEvent.error(img);

    expect(await screen.findByText('Finish photo unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: /first article finish photo/i })).not.toBeInTheDocument();
  });

  it('keeps the camera collapsed by default and mounts CameraTile on demand', async () => {
    const user = userEvent.setup();
    render(
      <FirstArticleBanner
        run={run({ first_article_printer_id: 3, first_article_printer_name: 'H2S-Alpha' })}
      />,
    );

    await screen.findByText('First article awaiting approval');
    // Collapsed by default — no stream pulled unprompted.
    expect(screen.queryByTestId('camera-tile')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /view camera/i }));
    expect(await screen.findByTestId('camera-tile')).toBeInTheDocument();
    expect(screen.getByText('Printer: H2S-Alpha')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /hide camera/i }));
    await waitFor(() => expect(screen.queryByTestId('camera-tile')).not.toBeInTheDocument());
  });

  it('renders the photo in the rejected state too', async () => {
    render(
      <FirstArticleBanner
        run={run({
          first_article_state: 'rejected',
          first_article_reject_reason: 'warped corners',
          first_article_photo_url: '/api/v1/archives/9/photos/finish_2.jpg',
          first_article_printer_id: 3,
        })}
      />,
    );

    expect(await screen.findByText('First article rejected')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /first article finish photo/i })).toBeInTheDocument();
  });

  it('approves (physical) with eject_remotely=false after confirming', async () => {
    let approveBody: Record<string, unknown> | null = null;
    server.use(
      http.post('*/api/v1/production-runs/:id/first-article/approve', async ({ request }) => {
        approveBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    render(<FirstArticleBanner run={run()} />);

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
      http.post('*/api/v1/production-runs/:id/first-article/reject', () => {
        rejectCalls += 1;
        return HttpResponse.json({ detail: 'run is not awaiting approval' }, { status: 409 });
      }),
    );

    const user = userEvent.setup();
    render(<FirstArticleBanner run={run()} />);

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
