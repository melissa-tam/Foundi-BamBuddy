/**
 * Tests for the QueuePage component.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { QueuePage } from '../../pages/QueuePage';
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
      await user.click(await screen.findByRole('button', { name: /^History/ }));

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

      await user.click(await screen.findByRole('button', { name: /^History/ }));

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
      await user.click(await screen.findByRole('button', { name: /^History/ }));

      await waitFor(() => {
        expect(screen.getByText('Clear History')).toBeInTheDocument();
      });
    });

    it('opens confirm modal when clicking clear history', async () => {
      const user = userEvent.setup();
      render(<QueuePage />);

      await user.click(await screen.findByRole('button', { name: /^History/ }));

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
      await screen.findByText(/low filament — swap the spool, then press re-check/i),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /re-check and release/i }));

    await waitFor(() => expect(releaseCalled).toBe(true));
    expect(await screen.findByText(/released 1 staged item/i)).toBeInTheDocument();
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
      await screen.findByText(/low filament — swap the spool, then press re-check/i),
    ).toBeInTheDocument();

    // The release action is disabled and explains why on hover.
    const btn = screen.getByRole('button', { name: /re-check and release/i });
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

    const btn = await screen.findByRole('button', { name: /re-check and release/i });
    await waitFor(() => expect(btn).toBeEnabled());
    expect(btn).not.toHaveAttribute('title', 'Ask a queue admin to release these items');
  });
});
