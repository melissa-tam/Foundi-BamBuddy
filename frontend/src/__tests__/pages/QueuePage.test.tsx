/**
 * Tests for the QueuePage component.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { render } from '../utils';
import { QueuePage } from '../../pages/QueuePage';
import { api } from '../../api/client';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

// Mock queue data
const mockQueueItems = [
  {
    id: 1,
    printer_id: 1,
    archive_id: 1,
    position: 1,
    status: 'pending',
    scheduled_time: null,
    require_previous_success: false,
    auto_off_after: false,
    manual_start: false,
    ams_mapping: null,
    plate_id: null,
    bed_levelling: true,
    flow_cali: false,
    vibration_cali: true,
    layer_inspect: false,
    timelapse: false,
    use_ams: true,
    started_at: null,
    completed_at: null,
    error_message: null,
    created_at: '2024-01-01T00:00:00Z',
    archive_name: 'Test Print 1',
    archive_thumbnail: '/thumb1.png',
    printer_name: 'Test Printer',
    print_time_seconds: 3600,
  },
  {
    id: 2,
    printer_id: 1,
    archive_id: 2,
    position: 2,
    status: 'printing',
    scheduled_time: null,
    require_previous_success: false,
    auto_off_after: true,
    manual_start: false,
    ams_mapping: null,
    plate_id: null,
    bed_levelling: true,
    flow_cali: false,
    vibration_cali: true,
    layer_inspect: false,
    timelapse: false,
    use_ams: true,
    started_at: '2024-01-01T10:00:00Z',
    completed_at: null,
    error_message: null,
    created_at: '2024-01-01T00:00:00Z',
    archive_name: 'Active Print',
    archive_thumbnail: '/thumb2.png',
    printer_name: 'Test Printer',
    print_time_seconds: 7200,
  },
  {
    id: 3,
    printer_id: 1,
    archive_id: 3,
    position: 3,
    status: 'completed',
    scheduled_time: null,
    require_previous_success: false,
    auto_off_after: false,
    manual_start: false,
    ams_mapping: null,
    plate_id: null,
    bed_levelling: true,
    flow_cali: false,
    vibration_cali: true,
    layer_inspect: false,
    timelapse: false,
    use_ams: true,
    started_at: '2024-01-01T08:00:00Z',
    completed_at: '2024-01-01T09:00:00Z',
    error_message: null,
    created_at: '2024-01-01T00:00:00Z',
    archive_name: 'Completed Print',
    archive_thumbnail: '/thumb3.png',
    printer_name: 'Test Printer',
    print_time_seconds: 1800,
  },
];

const mockPrinters = [
  {
    id: 1,
    name: 'Test Printer',
    ip_address: '192.168.1.100',
    serial_number: 'TESTSERIAL0001',
    access_code: '12345678',
    model: 'X1C',
    enabled: true,
    created_at: '2024-01-01T00:00:00Z',
  },
];

describe('QueuePage', () => {
  beforeEach(() => {
    // Mock localStorage.getItem to return expected defaults for queue page
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => {
      if (key === 'queue.historyCollapsed') return 'false'; // expanded
      if (key === 'queue.viewMode') return 'list';
      return null;
    });

    // Setup MSW handlers for this test
    server.use(
      http.get('/api/v1/queue/', () => {
        return HttpResponse.json(mockQueueItems);
      }),
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json(mockPrinters);
      }),
      http.delete('/api/v1/queue/:id', () => {
        return HttpResponse.json({ success: true });
      }),
      http.post('/api/v1/queue/:id/cancel', () => {
        return HttpResponse.json({ success: true });
      }),
      http.post('/api/v1/queue/:id/start', () => {
        return HttpResponse.json({ success: true });
      }),
      http.post('/api/v1/queue/:id/stop', () => {
        return HttpResponse.json({ success: true });
      }),
      http.post('/api/v1/queue/reorder', () => {
        return HttpResponse.json({ success: true });
      })
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Print Queue')).toBeInTheDocument();
      });
    });

    it('renders the page description', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Schedule and manage your print jobs')).toBeInTheDocument();
      });
    });

    it('shows summary cards', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        // Check for the page title (Print Queue is the h1)
        expect(screen.getByText('Print Queue')).toBeInTheDocument();
      });
    });

    it('shows filter dropdowns', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('All Printers')).toBeInTheDocument();
        expect(screen.getByText('All Status')).toBeInTheDocument();
      });
    });
  });

  describe('queue items display', () => {
    it('shows pending queue items', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Test Print 1')).toBeInTheDocument();
      });
    });

    it('shows active printing items', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Active Print')).toBeInTheDocument();
        expect(screen.getByText('Currently Printing')).toBeInTheDocument();
      });
    });

    it('shows completed items in history', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      // The History tab now owns the completed/cancelled/failed list.
      await user.click(await screen.findByRole('tab', { name: /^History/ }));

      await waitFor(() => {
        expect(screen.getByText('Completed Print')).toBeInTheDocument();
      });
    });

    it('shows status badges', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        // Queue items should be visible with status indicators
        expect(screen.getByText('Test Print 1')).toBeInTheDocument();
      });
    });

    it('shows printer names', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        const printerElements = screen.getAllByText('Test Printer');
        expect(printerElements.length).toBeGreaterThan(0);
      });
    });

    it('renders queue items with plate_id correctly', async () => {
      // Override with queue items that have plate_id set
      server.use(
        http.get('/api/v1/queue/', () => {
          return HttpResponse.json([
            {
              ...mockQueueItems[0],
              plate_id: 2,
              archive_name: 'Multi-plate Print',
            },
          ]);
        })
      );

      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Multi-plate Print')).toBeInTheDocument();
      });
    });
  });

  describe('empty state', () => {
    it('shows empty state when no queue items', async () => {
      server.use(
        http.get('/api/v1/queue/', () => {
          return HttpResponse.json([]);
        })
      );

      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('No prints scheduled')).toBeInTheDocument();
      });
    });
  });

  describe('filtering', () => {
    it('has printer filter options', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('All Printers')).toBeInTheDocument();
      });

      const printerSelect = screen.getByDisplayValue('All Printers');
      await user.click(printerSelect);

      expect(screen.getByText('Unassigned')).toBeInTheDocument();
    });

    it('has status filter options', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('All Status')).toBeInTheDocument();
      });

      const statusSelect = screen.getByDisplayValue('All Status');
      await user.click(statusSelect);

      expect(screen.getByRole('option', { name: 'Pending' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'Printing' })).toBeInTheDocument();
      expect(screen.getByRole('option', { name: 'Completed' })).toBeInTheDocument();
    });
  });

  describe('queue actions', () => {
    it('shows edit button for pending items', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Test Print 1')).toBeInTheDocument();
      });

      // Find the edit button (Pencil icon)
      const editButtons = screen.getAllByTitle('Edit');
      expect(editButtons.length).toBeGreaterThan(0);
    });

    it('shows cancel button for pending items', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Test Print 1')).toBeInTheDocument();
      });

      const cancelButtons = screen.getAllByTitle('Cancel');
      expect(cancelButtons.length).toBeGreaterThan(0);
    });

    it('shows stop button for printing items', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Active Print')).toBeInTheDocument();
      });

      const stopButtons = screen.getAllByTitle('Stop Print');
      expect(stopButtons.length).toBeGreaterThan(0);
    });

    it('shows re-queue button for history items', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      await user.click(await screen.findByRole('tab', { name: /^History/ }));

      await waitFor(() => {
        expect(screen.getByText('Completed Print')).toBeInTheDocument();
      });

      const requeueButtons = screen.getAllByTitle('Re-queue');
      expect(requeueButtons.length).toBeGreaterThan(0);
    });
  });

  describe('clear history', () => {
    it('shows clear history button when history exists', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      // Clear History only renders inside the History tab now.
      await user.click(await screen.findByRole('tab', { name: /^History/ }));

      await waitFor(() => {
        expect(screen.getByText('Clear History')).toBeInTheDocument();
      });
    });

    it('opens confirm modal when clicking clear history', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      await user.click(await screen.findByRole('tab', { name: /^History/ }));

      await waitFor(() => {
        expect(screen.getByText('Clear History')).toBeInTheDocument();
      });

      const clearButton = screen.getByRole('button', { name: /clear history/i });
      await user.click(clearButton);

      await waitFor(() => {
        expect(screen.getByText(/Are you sure you want to remove all/i)).toBeInTheDocument();
      });
    });
  });

  describe('staged items', () => {
    it('shows staged badge for manual_start items', async () => {
      server.use(
        http.get('/api/v1/queue/', () => {
          return HttpResponse.json([
            {
              ...mockQueueItems[0],
              manual_start: true,
            },
          ]);
        })
      );

      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Staged')).toBeInTheDocument();
      });
    });

    it('shows start button for staged items', async () => {
      server.use(
        http.get('/api/v1/queue/', () => {
          return HttpResponse.json([
            {
              ...mockQueueItems[0],
              manual_start: true,
            },
          ]);
        })
      );

      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByTitle('Start Print')).toBeInTheDocument();
      });
    });
  });

  describe('auto power off badge', () => {
    it('shows power off badge when auto_off_after is true', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Auto power off')).toBeInTheDocument();
      });
    });
  });

  describe('gcode injection badge', () => {
    it('shows G-code badge when gcode_injection is true', async () => {
      const itemsWithGcode = mockQueueItems.map((item, i) =>
        i === 0 ? { ...item, gcode_injection: true } : item
      );
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json(itemsWithGcode)),
      );

      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('G-code')).toBeInTheDocument();
      });
    });

    it('does not show G-code badge when gcode_injection is false', async () => {
      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText('Test Print 1')).toBeInTheDocument();
      });

      expect(screen.queryByText('G-code')).not.toBeInTheDocument();
    });
  });

  describe('filament-short ▶ flow (#1496)', () => {
    /**
     * The dispatch pre-flight flags a queue item as filament_short. The user
     * clicks ▶, the backend re-checks live and either dispatches (no deficit
     * anymore — clear flag) or returns 409 with the per-slot deficit so the
     * frontend can render the "Print Anyway" confirm modal.
     */
    const shortItem = {
      ...mockQueueItems[0],
      manual_start: true,
      filament_short: true,
    };

    it('renders the filament-short badge on a flagged pending row', async () => {
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json([shortItem])),
      );

      render(<QueuePage />);

      await waitFor(() => {
        expect(screen.getByText(/Insufficient filament for the assigned spool/i)).toBeInTheDocument();
      });
    });

    it('opens the Print Anyway modal when ▶ returns 409 and retries with skip_filament_check', async () => {
      let secondCallSkippedCheck: boolean | null = null;
      let attempts = 0;
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json([shortItem])),
        http.post('/api/v1/queue/:id/start', ({ request }) => {
          attempts += 1;
          const url = new URL(request.url);
          const skip = url.searchParams.get('skip_filament_check') === 'true';
          if (attempts === 1) {
            return HttpResponse.json(
              {
                detail: {
                  code: 'insufficient_filament',
                  deficit: [
                    {
                      slot_id: 1,
                      ams_id: 0,
                      tray_id: 0,
                      filament_type: 'PLA',
                      required_grams: 270,
                      remaining_grams: 200,
                    },
                  ],
                },
              },
              { status: 409 },
            );
          }
          secondCallSkippedCheck = skip;
          return HttpResponse.json({ ...shortItem, manual_start: false, filament_short: false });
        }),
      );

      render(<QueuePage />);

      const playButton = await screen.findByTitle(/Start Print|do not have permission to start prints/i);
      await userEvent.click(playButton);

      // Wait for the start endpoint to be hit (the 409 path returns to onError).
      await waitFor(() => expect(attempts).toBe(1));
      // Modal shows the deficit detail
      await screen.findByRole('button', { name: /Print Anyway/i });
      expect(
        screen.getByText(/Slot 1: needs 270 g, 200 g remaining/i),
      ).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /Print Anyway/i }));

      await waitFor(() => expect(secondCallSkippedCheck).toBe(true));
      expect(attempts).toBe(2);
    });

    it('starts an already-acknowledged item straight through with no Print Anyway confirm (#1698-followup)', async () => {
      // Deficit was acknowledged at queue creation; the stored flag rides on
      // the row. Clicking ▶ must dispatch directly — no redundant re-ask.
      const ackedItem = {
        ...mockQueueItems[0],
        manual_start: true,
        filament_short: true,
        skip_filament_check: true,
      };
      let firstCallSkippedCheck: boolean | null = null;
      let attempts = 0;
      server.use(
        http.get('/api/v1/queue/', () => HttpResponse.json([ackedItem])),
        http.post('/api/v1/queue/:id/start', ({ request }) => {
          attempts += 1;
          const url = new URL(request.url);
          firstCallSkippedCheck = url.searchParams.get('skip_filament_check') === 'true';
          return HttpResponse.json({ ...ackedItem, manual_start: false, filament_short: false });
        }),
      );

      render(<QueuePage />);

      const playButton = await screen.findByTitle(/Start Print|do not have permission to start prints/i);
      await userEvent.click(playButton);

      // The single start call carries the stored ack; no confirm dialog opens.
      await waitFor(() => expect(attempts).toBe(1));
      expect(firstCallSkippedCheck).toBe(true);
      expect(screen.queryByRole('button', { name: /Print Anyway/i })).not.toBeInTheDocument();
    });
  });
});


describe('QueuePage farm surfaces (Phase 4)', () => {
  const farmPending = {
    ...mockQueueItems[0],
    id: 10,
    status: 'pending',
    batch_id: 7,
    batch_name: 'SKU007 run',
    production_run_id: 7,
    archive_name: 'Farm Unit',
  };
  const farmPrinting = {
    ...mockQueueItems[1],
    id: 11,
    status: 'printing',
    batch_id: 7,
    batch_name: 'SKU007 run',
    production_run_id: 7,
    archive_name: 'Farm Active',
  };

  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => {
      if (key === 'queue.historyCollapsed') return 'false';
      if (key === 'queue.viewMode') return 'list';
      // Batch groups default collapsed; expand run 7's group so its child
      // rows (and their Run badges) are visible to the queries below.
      if (key === 'queue.batchCollapsed') return '{"7": false}';
      return null;
    });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
    );
  });

  it('shows the resume-after-failure banner on the machine code, not the English text', async () => {
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          {
            ...mockQueueItems[0],
            id: 20,
            status: 'skipped',
            waiting_reason: 'previous_print_failed',
            error_message: 'Texte localisé quelconque',
            printer_name: 'Test Printer',
          },
        ]),
      ),
    );

    render(<QueuePage />);

    expect(
      await screen.findByText(/blocked by a previous-print failure/i),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /resume after failure/i })).toBeInTheDocument();
  });

  it('does NOT show the banner for a skipped item without the machine code', async () => {
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          {
            ...mockQueueItems[0],
            id: 21,
            status: 'skipped',
            waiting_reason: null,
            error_message: 'Previous print failed or was aborted',
          },
        ]),
      ),
    );

    render(<QueuePage />);

    await screen.findByText('Print Queue');
    expect(screen.queryByText(/blocked by a previous-print failure/i)).not.toBeInTheDocument();
  });

  it('renders a Run badge linking to the run detail on farm rows', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([farmPending])));

    render(<QueuePage />);

    const badge = await screen.findByRole('link', { name: /run/i });
    expect(badge).toHaveAttribute('href', '/production-runs/7');
  });

  it('uses farm-aware copy in the stop confirm for run-managed prints', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([farmPrinting])));

    render(<QueuePage />);

    await screen.findByText('Farm Active');
    await userEvent.click(screen.getByTitle('Stop Print'));

    expect(await screen.findByText(/counts as an operator stop/i)).toBeInTheDocument();
    expect(screen.getByText(/no auto-retry/i)).toBeInTheDocument();
  });

  it('keeps the plain stop confirm for non-farm prints', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([mockQueueItems[1]])));

    render(<QueuePage />);

    await screen.findByText('Active Print');
    await userEvent.click(screen.getByTitle('Stop Print'));

    await screen.findByText(/are you sure you want to stop/i);
    expect(screen.queryByText(/counts as an operator stop/i)).not.toBeInTheDocument();
  });

  it('groups low-spool staged rows under a banner and releases via re-check', async () => {
    let releaseCalled = false;
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          { ...farmPending, manual_start: true, filament_short: true },
        ]),
      ),
      http.post('/api/v1/queue/release-staged', () => {
        releaseCalled = true;
        return HttpResponse.json({ released: 1 });
      }),
    );

    render(<QueuePage />);

    expect(
      await screen.findByText(/low filament — load a spool; the farm resumes automatically/i),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /re-check now/i }));

    await waitFor(() => expect(releaseCalled).toBe(true));
    expect(await screen.findByText(/released 1 staged item/i)).toBeInTheDocument();
  });

  it('lists distinct per-machine blocking reasons in the low-spool banner (D9)', async () => {
    // The scheduler persists a rich "Low filament: <printer> (...)" reason into
    // each staged row so the banner NAMES which machine to top up — the incident
    // was a bare "swap the spool" banner that sent the operator to the wrong one.
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          {
            ...farmPending,
            id: 10,
            manual_start: true,
            filament_short: true,
            waiting_reason: 'Low filament: 004-H2S (needs more filament)',
          },
          {
            ...farmPending,
            id: 12,
            manual_start: true,
            filament_short: true,
            waiting_reason: 'Low filament: 011-H2S (starting spool below minimum)',
          },
        ]),
      ),
    );

    render(<QueuePage />);

    // Banner-only heading proves the details block rendered; the reasons name
    // the specific short machines (they also appear on their rows → getAllByText).
    expect(await screen.findByText(/waiting on:/i)).toBeInTheDocument();
    expect(
      screen.getAllByText('Low filament: 004-H2S (needs more filament)').length,
    ).toBeGreaterThan(0);
    expect(
      screen.getAllByText('Low filament: 011-H2S (starting spool below minimum)').length,
    ).toBeGreaterThan(0);
  });

  // 5b: both farm banners are visible to any queue viewer; only the action
  // buttons stay behind queue:update_all (disabled + explanatory hint when
  // lacking). Low-priv = auth enabled with no logged-in user → hasPermission
  // returns false for everything.
  it('shows the low-spool banner to a low-privilege viewer with a disabled, hinted release button', async () => {
    server.use(
      http.get('*/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: true, requires_setup: false }),
      ),
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([{ ...farmPending, manual_start: true, filament_short: true }]),
      ),
    );

    render(<QueuePage />);

    // Banner renders regardless of permission.
    expect(
      await screen.findByText(/low filament — load a spool; the farm resumes automatically/i),
    ).toBeInTheDocument();

    // The release action is disabled and explains why on hover.
    const btn = screen.getByRole('button', { name: /re-check now/i });
    await waitFor(() => expect(btn).toBeDisabled());
    expect(btn).toHaveAttribute('title', 'Ask a queue admin to release these items');
  });

  it('shows the resume-after-failure banner to a low-privilege viewer with a disabled, hinted button', async () => {
    server.use(
      http.get('*/api/v1/auth/status', () =>
        HttpResponse.json({ auth_enabled: true, requires_setup: false }),
      ),
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          {
            ...mockQueueItems[0],
            id: 30,
            status: 'skipped',
            waiting_reason: 'previous_print_failed',
            printer_name: 'Test Printer',
          },
        ]),
      ),
    );

    render(<QueuePage />);

    expect(
      await screen.findByText(/blocked by a previous-print failure/i),
    ).toBeInTheDocument();

    const btn = screen.getByRole('button', { name: /resume after failure/i });
    await waitFor(() => expect(btn).toBeDisabled());
    expect(btn).toHaveAttribute('title', 'Ask a queue admin to release these items');
  });

  it('enables the release button for a queue admin (queue:update_all)', async () => {
    // Default auth (auth_enabled: false) grants all permissions.
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([{ ...farmPending, manual_start: true, filament_short: true }]),
      ),
    );

    render(<QueuePage />);

    const btn = await screen.findByRole('button', { name: /re-check now/i });
    await waitFor(() => expect(btn).toBeEnabled());
    expect(btn).not.toHaveAttribute('title', 'Ask a queue admin to release these items');
  });
});

// ---------------------------------------------------------------------------
// Printers-pool targets. A unit created against a SET of printers stays unpinned
// until the scheduler places it, so both the row and the by-printer bucket name
// the POOL rather than a printer — one origin, `utils/queueTarget`.
// ---------------------------------------------------------------------------
describe('QueuePage printers-pool targets', () => {
  const poolFleet = [
    { ...mockPrinters[0], id: 1, name: 'H2S-Alpha', model: 'H2S' },
    { ...mockPrinters[0], id: 2, name: 'H2C-Beta', model: 'H2C' },
  ];

  const poolItem = {
    ...mockQueueItems[0],
    id: 40,
    printer_id: null,
    printer_name: null,
    target_printer_ids: [1, 2],
    archive_name: 'Pool Unit',
  };

  const pinnedItem = {
    ...mockQueueItems[0],
    id: 41,
    printer_id: 2,
    printer_name: 'H2C-Beta',
    archive_name: 'Pinned Unit',
  };

  /** Printer names also fill the filter <select>; only rendered rows and
   *  bucket headers say anything about targets. */
  const namedOutsideFilter = (name: string) =>
    screen.getAllByText(name).filter((el) => el.tagName !== 'OPTION');

  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => {
      if (key === 'queue.historyCollapsed') return 'false';
      if (key === 'queue.viewMode') return 'list';
      return null;
    });
    server.use(http.get('/api/v1/printers/', () => HttpResponse.json(poolFleet)));
  });

  it('names every pool member on the row', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([poolItem])));

    render(<QueuePage />);

    await screen.findByText('Pool Unit');
    expect(screen.getByText('Any of H2S-Alpha, H2C-Beta')).toBeInTheDocument();
  });

  it('falls back to #id for a pool member no longer in the fleet', async () => {
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([{ ...poolItem, target_printer_ids: [1, 99] }]),
      ),
    );

    render(<QueuePage />);

    await screen.findByText('Pool Unit');
    expect(screen.getByText('Any of H2S-Alpha, #99')).toBeInTheDocument();
  });

  it('leaves a pinned row naming its own printer', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([pinnedItem])));

    render(<QueuePage />);

    await screen.findByText('Pinned Unit');
    // The printer-filter <select> also lists the fleet; only the row counts.
    expect(namedOutsideFilter('H2C-Beta')).toHaveLength(1);
    expect(screen.queryByText(/Any of/)).not.toBeInTheDocument();
  });

  it('buckets a pool unit under its own lane, apart from a pinned unit', async () => {
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json([poolItem, pinnedItem])));

    const user = userEvent.setup();
    render(<QueuePage />);

    await screen.findByText('Pool Unit');
    // Flat list: the pool label appears once, on the row itself.
    expect(screen.getAllByText('Any of H2S-Alpha, H2C-Beta')).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'By Printer' }));

    // Grouped: the row plus its own bucket header — the pool is a lane, not
    // merged into the pinned printer's bucket or into unassigned.
    await waitFor(() =>
      expect(screen.getAllByText('Any of H2S-Alpha, H2C-Beta')).toHaveLength(2),
    );
    // The pinned unit keeps its own bucket: its row plus that bucket's header.
    expect(namedOutsideFilter('H2C-Beta')).toHaveLength(2);
    // A pool is a target, so nothing lands in the unassigned bucket.
    expect(namedOutsideFilter('Unassigned')).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Queue ORDER and FILTERS. The page owns neither: order comes from
// `utils/queueSort`, the predicates from `utils/queueFilter`. These drive the
// real controls and assert what the DOM ends up showing.
// ---------------------------------------------------------------------------
describe('QueuePage sorting', () => {
  // Position order C, A, B — so every key produces a visibly different list.
  const sortFleet = [{ ...mockPrinters[0], id: 1, name: 'Zulu' }, { ...mockPrinters[0], id: 2, name: 'Alpha' }];
  const sortItems = [
    { ...mockQueueItems[0], id: 51, position: 1, archive_name: 'Unit C', printer_id: 1, printer_name: 'Zulu', scheduled_time: '2026-03-01T10:00:00Z' },
    { ...mockQueueItems[0], id: 52, position: 2, archive_name: 'Unit A', printer_id: 2, printer_name: 'Alpha', scheduled_time: '2026-01-01T10:00:00Z' },
    { ...mockQueueItems[0], id: 53, position: 3, archive_name: 'Unit B', printer_id: 1, printer_name: 'Zulu', scheduled_time: '2026-02-01T10:00:00Z' },
  ];

  const shownUnits = () => screen.getAllByText(/^Unit [A-Z]$/).map((el) => el.textContent);

  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation(() => null);
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(sortFleet)),
      http.get('/api/v1/queue/', () => HttpResponse.json(sortItems)),
    );
  });

  it('starts in stored position order', async () => {
    render(<QueuePage />);
    await screen.findByText('Unit C');
    // Position lanes by TARGET first: Zulu's two units in position order, then
    // Alpha's — the same lanes the scheduler drains.
    expect(shownUnits()).toEqual(['Unit C', 'Unit B', 'Unit A']);
  });

  it('reorders the DOM for every sort key, and for the direction toggle', async () => {
    const user = userEvent.setup();
    render(<QueuePage />);
    await screen.findByText('Unit C');
    const sortSelect = screen.getByDisplayValue('Sort by Position');

    await user.selectOptions(sortSelect, 'name');
    await waitFor(() => expect(shownUnits()).toEqual(['Unit A', 'Unit B', 'Unit C']));

    // The button names the CURRENT direction (the page's existing reading);
    // clicking it flips the list.
    await user.click(screen.getByRole('button', { name: 'Sort ascending' }));
    await waitFor(() => expect(shownUnits()).toEqual(['Unit C', 'Unit B', 'Unit A']));

    await user.click(screen.getByRole('button', { name: 'Sort descending' }));
    // By printer: Alpha's unit first, then Zulu's two in position order.
    await user.selectOptions(sortSelect, 'printer');
    await waitFor(() => expect(shownUnits()).toEqual(['Unit A', 'Unit C', 'Unit B']));

    await user.selectOptions(sortSelect, 'time');
    await waitFor(() => expect(shownUnits()).toEqual(['Unit A', 'Unit B', 'Unit C']));
  });

  it('sorts the history tab and names its direction by the date reading', async () => {
    const user = userEvent.setup();
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          { ...sortItems[0], id: 61, status: 'completed', archive_name: 'Unit C', completed_at: '2026-01-03T00:00:00Z' },
          { ...sortItems[1], id: 62, status: 'completed', archive_name: 'Unit A', completed_at: '2026-01-01T00:00:00Z' },
          { ...sortItems[2], id: 63, status: 'completed', archive_name: 'Unit B', completed_at: '2026-01-02T00:00:00Z' },
        ]),
      ),
    );

    render(<QueuePage />);
    await user.click(await screen.findByRole('tab', { name: /^History/ }));

    // Default is newest first.
    await waitFor(() => expect(shownUnits()).toEqual(['Unit C', 'Unit B', 'Unit A']));
    await user.click(screen.getByRole('button', { name: 'Descending (newest first)' }));
    await waitFor(() => expect(shownUnits()).toEqual(['Unit A', 'Unit B', 'Unit C']));
    await waitFor(() => expect(shownUnits()).toEqual(['Unit A', 'Unit B', 'Unit C']));

    // A non-date key drops the oldest/newest wording.
    await user.selectOptions(screen.getByDisplayValue('Sort by Date'), 'name');
    expect(await screen.findByRole('button', { name: 'Sort ascending' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// The STORED order is what a drag posts back, so what the page displays under
// the Position sort has to be the order the backend numbered — laned on the
// position SCOPE (`utils/queueTarget.queuePositionScopeKey`), never on target
// identity. The 09-19 defect: an appended "Any <model>" run rendered at the top
// of a queue of printer-subset runs, and the next drop stored that reading.
// ---------------------------------------------------------------------------
/**
 * dnd-kit measures droppables with `getBoundingClientRect`, and jsdom answers
 * every element with a zero rect — so a drag finds no drop target and the drop
 * is a no-op. Give the document a plain vertical stack (10px rows in DOM
 * order) for the duration of one drag; the caller restores it.
 */
function stubVerticalRects(): () => void {
  const original = Element.prototype.getBoundingClientRect;
  Element.prototype.getBoundingClientRect = function (this: Element): DOMRect {
    const top = Array.from(document.querySelectorAll('*')).indexOf(this) * 10;
    return {
      x: 0,
      y: top,
      top,
      left: 0,
      bottom: top + 10,
      right: 300,
      width: 300,
      height: 10,
      toJSON: () => ({}),
    } as DOMRect;
  };
  return () => {
    Element.prototype.getBoundingClientRect = original;
  };
}

describe('QueuePage stored order across target shapes', () => {
  const scopeFleet = [
    { ...mockPrinters[0], id: 1, name: 'H2S-Alpha', model: 'H2S' },
    { ...mockPrinters[0], id: 2, name: 'H2C-Beta', model: 'H2C' },
  ];

  /** Positions 1..4 in ONE shared sequence, across all four target shapes.
   *  The model pool is APPENDED — highest position, so it renders LAST. */
  const scopeItems = [
    {
      ...mockQueueItems[0],
      id: 71,
      position: 1,
      printer_id: null,
      printer_name: null,
      target_printer_ids: [1, 2],
      archive_name: 'Unit A',
    },
    {
      ...mockQueueItems[0],
      id: 72,
      position: 2,
      printer_id: null,
      printer_name: null,
      archive_name: 'Unit B',
    },
    {
      ...mockQueueItems[0],
      id: 73,
      position: 3,
      printer_id: null,
      printer_name: null,
      target_model: 'H2S',
      archive_name: 'Unit C',
    },
  ];

  const shownUnits = () => screen.getAllByText(/^Unit [A-Z]$/).map((el) => el.textContent);

  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation(() => null);
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(scopeFleet)),
      http.get('/api/v1/queue/', () => HttpResponse.json(scopeItems)),
    );
  });

  it('renders a mixed-pool pending list in POSITION order', async () => {
    render(<QueuePage />);
    await screen.findByText('Unit A');
    expect(shownUnits()).toEqual(['Unit A', 'Unit B', 'Unit C']);
  });

  it('posts the DISPLAYED order as ordered_ids after a drag', async () => {
    let body: { ordered_ids: number[] } | null = null;
    server.use(
      http.post('/api/v1/queue/reorder', async ({ request }) => {
        body = (await request.json()) as { ordered_ids: number[] };
        return HttpResponse.json({ message: 'ok' });
      }),
    );

    const restoreRects = stubVerticalRects();
    try {
      const user = userEvent.setup();
      render(<QueuePage />);
      await screen.findByText('Unit A');
      expect(shownUnits()).toEqual(['Unit A', 'Unit B', 'Unit C']);

      // Keyboard drag on the first row's handle: lift, move, drop.
      const handle = screen
        .getAllByRole('button')
        .find((el) => el.getAttribute('aria-roledescription') === 'sortable');
      expect(handle).toBeDefined();
      handle!.focus();
      await user.keyboard(' ');
      await user.keyboard('{ArrowDown}');
      await user.keyboard(' ');

      await waitFor(() => expect(body).not.toBeNull());
    } finally {
      restoreRects();
    }

    const ids = body!.ordered_ids;
    // Nothing is dropped from the list, and the drop was honoured — a
    // cross-target-shape drag used to snap back, because the lane outranked
    // the position.
    expect([...ids].sort((a, b) => a - b)).toEqual([71, 72, 73]);
    expect(ids[0]).not.toBe(71);
    // The rows the operator did NOT touch keep the order they were DISPLAYED
    // in — which is the stored position order. Laned by target identity they
    // would go back as the model pool ahead of the unassigned row.
    expect(ids.indexOf(72)).toBeLessThan(ids.indexOf(73));
  });

  it('shows the target on a COLLAPSED run row', async () => {
    // A run row is collapsed by default, so without a chip on the header the
    // operator could not see what the run targets without expanding it.
    server.use(
      http.get('/api/v1/queue/', () =>
        HttpResponse.json([
          { ...scopeItems[2], id: 81, batch_id: 5, batch_name: 'SKU007 run', archive_name: 'Unit D' },
          { ...scopeItems[2], id: 82, batch_id: 5, batch_name: 'SKU007 run', archive_name: 'Unit E' },
        ]),
      ),
    );

    render(<QueuePage />);
    await screen.findByText('SKU007 run');

    // Collapsed: the children are not rendered, so the one label is the
    // header's own chip.
    expect(screen.queryByText('Unit D')).not.toBeInTheDocument();
    expect(screen.getAllByText('Any H2S')).toHaveLength(1);
  });

  it('gives each target its own labelled group in the by-target layout', async () => {
    const user = userEvent.setup();
    render(<QueuePage />);
    await screen.findByText('Unit A');

    await user.click(screen.getByRole('button', { name: 'By Printer' }));

    // One heading per distinct target — the group names itself, and says so in
    // the outline as well as in colour.
    expect(
      await screen.findByRole('heading', { level: 3, name: 'Any of H2S-Alpha, H2C-Beta' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3, name: 'Any H2S' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 3, name: 'Unassigned' })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(3);
  });
});

describe('QueuePage filters', () => {
  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation(() => null);
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      http.get('/api/v1/queue/', () => HttpResponse.json(mockQueueItems)),
    );
  });

  it('applies the status filter as a VIEW — one fetch, not a narrowed one', async () => {
    const user = userEvent.setup();
    const statuses: (string | null)[] = [];
    server.use(
      http.get('/api/v1/queue/', ({ request }) => {
        statuses.push(new URL(request.url).searchParams.get('status'));
        return HttpResponse.json(mockQueueItems);
      }),
    );

    render(<QueuePage />);
    await screen.findByText('Test Print 1');
    await user.selectOptions(screen.getByDisplayValue('All Status'), 'completed');

    // The page never asks the server to narrow by status.
    await waitFor(() => expect(screen.queryByText('Test Print 1')).not.toBeInTheDocument());
    expect(statuses.every((s) => s === null)).toBe(true);
  });

  it('shows the filtered-empty card, and Clear filters brings the rows back', async () => {
    const user = userEvent.setup();
    render(<QueuePage />);
    await screen.findByText('Test Print 1');

    // Nothing pending or printing is "completed" — the queue tab empties.
    await user.selectOptions(screen.getByDisplayValue('All Status'), 'completed');
    expect(await screen.findByText('No items match the filters')).toBeInTheDocument();
    // NOT the "queue is empty" copy: the queue has rows, the filters hid them.
    expect(screen.queryByText('No prints scheduled')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(await screen.findByText('Test Print 1')).toBeInTheDocument();
  });

  it('shows the filtered-empty card on the history tab too', async () => {
    const user = userEvent.setup();
    render(<QueuePage />);
    await user.click(await screen.findByRole('tab', { name: /^History/ }));
    await screen.findByText('Completed Print');

    await user.selectOptions(screen.getByDisplayValue('All Status'), 'pending');
    expect(await screen.findByText('No items match the filters')).toBeInTheDocument();
  });
});

describe('QueuePage history paging', () => {
  // 60 terminal units: more than the old hard `slice(0, 50)`, which simply
  // dropped everything past the 50th with no way to reach it.
  const many = Array.from({ length: 60 }, (_, i) => ({
    ...mockQueueItems[2],
    id: 100 + i,
    archive_name: `Hist ${i + 1}`,
    completed_at: `2026-01-01T00:${String(i).padStart(2, '0')}:00Z`,
  }));

  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation(() => null);
    server.use(http.get('/api/v1/queue/', () => HttpResponse.json(many)));
  });

  it('pages past 50 instead of truncating', async () => {
    const user = userEvent.setup();
    render(<QueuePage />);
    await user.click(await screen.findByRole('tab', { name: /^History/ }));

    // Newest first: page 1 ends at Hist 11, so Hist 1 is on page 2.
    expect(await screen.findByText('Hist 60')).toBeInTheDocument();
    expect(screen.queryByText('Hist 1')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Next page' }));

    expect(await screen.findByText('Hist 1')).toBeInTheDocument();
    expect(screen.queryByText('Hist 60')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'First page' }));
    expect(await screen.findByText('Hist 60')).toBeInTheDocument();
  });
});

describe('QueuePage reorder', () => {
  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation(() => null);
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      http.get('/api/v1/queue/', () => HttpResponse.json(mockQueueItems)),
    );
  });

  it('offers the drag handle under the stored order', async () => {
    render(<QueuePage />);
    await screen.findByText('Test Print 1');
    expect(screen.queryByTitle(/Reordering needs the Position sort/)).not.toBeInTheDocument();
  });

  it('disables dragging under any other sort, and says why', async () => {
    const user = userEvent.setup();
    render(<QueuePage />);
    await screen.findByText('Test Print 1');

    await user.selectOptions(screen.getByDisplayValue('Sort by Position'), 'name');

    await waitFor(() =>
      expect(
        screen.getAllByTitle('Reordering needs the Position sort, ascending, with SJF off').length,
      ).toBeGreaterThan(0),
    );
  });

  it('disables dragging when the displayed order is the SJF reading', async () => {
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ queue_shortest_first: true })),
    );

    render(<QueuePage />);
    await screen.findByText('Test Print 1');

    await waitFor(() =>
      expect(
        screen.getAllByTitle('Reordering needs the Position sort, ascending, with SJF off').length,
      ).toBeGreaterThan(0),
    );
  });

  it('sends the display order as ids only — the server owns positions', async () => {
    let body: unknown = null;
    server.use(
      http.post('/api/v1/queue/reorder', async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ message: 'ok' });
      }),
    );

    await api.reorderQueue([7, 3, 11]);

    expect(body).toEqual({ ordered_ids: [7, 3, 11] });
  });
});

describe('QueuePage owns no queue comparator', () => {
  /**
   * Display order has ONE owner (`utils/queueSort`). The page may not grow a
   * second reading of it — that is how the flat list, the batch children and
   * the bucket lanes drifted apart in the first place.
   *
   * The banner comparator below is allowlisted BY ITS TEXT: it orders the
   * resume-after-failure banner's PRINTERS by name, not queue rows, so it is
   * not queue order at all. Same idiom as the backend's AST call-site
   * allowlists — a new comparator has to be argued for here before it lands.
   */
  const ALLOWED_INLINE_COMPARATORS = ['a.printerName.localeCompare(b.printerName)'];

  const readSource = (): string =>
    fs.readFileSync(
      path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../pages/QueuePage.tsx'),
      'utf8',
    );

  it('declares every inline comparator left in QueuePage.tsx', () => {
    const source = readSource();
    const offenders = source
      .split('\n')
      .filter((line) => line.includes('.sort(('))
      .filter((line) => !ALLOWED_INLINE_COMPARATORS.some((allowed) => line.includes(allowed)));

    expect(offenders).toEqual([]);
  });

  it('reads its order from the one origin', () => {
    const source = readSource();
    expect(source).toContain("from '../utils/queueSort'");
    expect(source).toContain("from '../utils/queueFilter'");
  });
});
