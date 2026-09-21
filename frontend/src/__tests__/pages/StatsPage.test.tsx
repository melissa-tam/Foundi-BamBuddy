/**
 * Tests for the StatsPage component.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { StatsPage } from '../../pages/StatsPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import i18n from '../../i18n';
import { FLEET_DASHBOARD_STORAGE_KEY } from '../../utils/fleetMetrics';
import {
  makeFleetOverviewDay,
  makeFleetOverviewFirstRun,
  makeFleetOverviewHour,
  makeFleetOverviewWeek,
  makeFleetStatus,
  makeFleetStatusFirstRun,
  makePrinterIntervals,
} from '../fixtures/fleetMetrics';

/** The Prints grid's layout key, spelled here so the reset test can prove it is untouched. */
const PRINTS_DASHBOARD_STORAGE_KEY = 'bambusy-dashboard-layout-v2';

// Complete mock stats matching ArchiveStats interface
const mockStats = {
  total_prints: 150,
  successful_prints: 140,
  failed_prints: 10,
  cancelled_prints: 0,
  total_print_time_hours: 500.5,
  total_filament_grams: 5500,
  total_cost: 125.50,
  prints_by_filament_type: {
    'PLA': 80,
    'PETG': 50,
    'ABS': 20,
  },
  prints_by_printer: {
    '1': 100,
    '2': 50,
  },
  average_time_accuracy: 98.5,
  time_accuracy_by_printer: {
    '1': 99.0,
    '2': 97.0,
  },
  total_energy_kwh: 45.5,
  total_energy_cost: 12.50,
};

const mockPrinters = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', enabled: true },
  { id: 2, name: 'P1S', model: 'P1S', enabled: true },
];

const mockArchives = [
  {
    id: 1,
    created_at: '2024-01-01T10:00:00Z',
    started_at: '2024-01-01T10:00:00Z',
    completed_at: '2024-01-01T14:30:00Z',
    print_name: 'Benchy',
    status: 'completed',
    printer_id: 1,
    filament_type: 'PLA',
    filament_color: '#00FF00',
    filament_used_grams: 25,
    actual_time_seconds: 16200,
    print_time_seconds: 15000,
    cost: 0.75,
    quantity: 1,
  },
  {
    id: 2,
    created_at: '2024-01-02T14:00:00Z',
    started_at: '2024-01-02T14:00:00Z',
    completed_at: '2024-01-02T22:00:00Z',
    print_name: 'Large Vase',
    status: 'completed',
    printer_id: 1,
    filament_type: 'PETG',
    filament_color: '#FF0000',
    filament_used_grams: 180,
    actual_time_seconds: 28800,
    print_time_seconds: 27000,
    cost: 5.40,
    quantity: 1,
  },
  {
    id: 3,
    created_at: '2024-01-03T08:00:00Z',
    started_at: '2024-01-03T08:00:00Z',
    completed_at: null,
    print_name: 'Failed Bracket',
    status: 'failed',
    printer_id: 2,
    filament_type: 'ABS',
    filament_color: '#0000FF',
    filament_used_grams: 10,
    actual_time_seconds: 3600,
    print_time_seconds: 7200,
    cost: 0.30,
    quantity: 1,
  },
  {
    id: 4,
    created_at: '2024-01-03T20:00:00Z',
    started_at: '2024-01-03T20:00:00Z',
    completed_at: '2024-01-04T02:00:00Z',
    print_name: 'Phone Stand',
    status: 'completed',
    printer_id: 2,
    filament_type: 'PLA',
    filament_color: '#00FF00',
    filament_used_grams: 45,
    actual_time_seconds: 21600,
    print_time_seconds: 20000,
    cost: 1.35,
    quantity: 1,
  },
];

const mockSettings = {
  currency: 'USD',
  check_updates: false,
  check_printer_firmware: false,
};

const mockFailureAnalysis = {
  period_days: 30,
  total_prints: 100,
  failed_prints: 5,
  failure_rate: 5.0,
  failures_by_reason: {
    'First layer adhesion': 3,
    'Filament runout': 2,
  },
  failures_by_filament: {
    'ABS': 3,
    'PLA': 2,
  },
  failures_by_printer: {
    '1': 2,
    '2': 3,
  },
  failures_by_hour: {},
  recent_failures: [],
  trend: [
    { week_start: '2024-01-01', total_prints: 50, failed_prints: 3, failure_rate: 6.0 },
    { week_start: '2024-01-08', total_prints: 50, failed_prints: 2, failure_rate: 5.0 },
  ],
};

describe('StatsPage', () => {
  beforeEach(() => {
    // The tab selection lives in the URL, and `BrowserRouter` reads the real
    // one — so a test that deep-links must not leak its search into the next.
    window.history.replaceState({}, '', '/');
    server.use(
      http.get('/api/v1/archives/stats', () => {
        return HttpResponse.json(mockStats);
      }),
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json(mockPrinters);
      }),
      http.get('/api/v1/archives/slim', () => {
        return HttpResponse.json(mockArchives);
      }),
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json(mockSettings);
      }),
      http.get('/api/v1/archives/analysis/failures', () => {
        return HttpResponse.json(mockFailureAnalysis);
      })
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Statistics')).toBeInTheDocument();
      });
    });

    it('shows quick stats widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Quick Stats')).toBeInTheDocument();
      });
    });

    it('shows total prints stat', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Total Prints')).toBeInTheDocument();
        expect(screen.getByText('150')).toBeInTheDocument();
      });
    });

    it('shows print time stat', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Time')).toBeInTheDocument();
        expect(screen.getByText('500.5h')).toBeInTheDocument();
      });
    });

    it('shows filament used stat', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament Used')).toBeInTheDocument();
        expect(screen.getByText('5.5kg')).toBeInTheDocument();
      });
    });
  });

  describe('success rate', () => {
    it('shows success rate widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success Rate')).toBeInTheDocument();
        // Success rate: 140 / 150 total = 93%
        expect(screen.getByText('93%')).toBeInTheDocument();
      });
    });

    it('excludes cancelled prints from the rate denominator and surfaces them in the breakdown (#1390)', async () => {
      // 40 completed, 20 failed (printer-detected), 40 user-cancelled out of
      // 100 total. The earlier fix divided by total_prints and reported 40%,
      // which conflated user intent with print quality — cancelling a roll
      // because you changed your mind shouldn't be counted against the
      // printer's success rate. New behaviour: gauge = 40 / (40 + 20) = 67%;
      // cancelled count still visible in the row breakdown so the missing
      // 40 prints don't silently disappear.
      server.use(
        http.get('/api/v1/archives/stats', () =>
          HttpResponse.json({
            ...mockStats,
            total_prints: 100,
            successful_prints: 40,
            failed_prints: 20,
            cancelled_prints: 35,
          }),
        ),
      );
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success Rate')).toBeInTheDocument();
        expect(screen.getByText('67%')).toBeInTheDocument();
        // Cancelled count surfaces in the breakdown so the missing prints
        // aren't silently swallowed (was the original bug in #1390). Pick a
        // value distinct from successful_prints/failed_prints to keep the
        // getByText query unambiguous.
        expect(screen.getByText('Cancelled:')).toBeInTheDocument();
        expect(screen.getByText('35')).toBeInTheDocument();
      });
    });
  });

  describe('cost display', () => {
    it('shows filament cost', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament Cost')).toBeInTheDocument();
      });
    });

    it('shows energy cost', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Energy Cost')).toBeInTheDocument();
      });
    });
  });

  describe('widgets', () => {
    it('shows time accuracy widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Time Accuracy')).toBeInTheDocument();
      });
    });

    it('shows print activity widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Activity')).toBeInTheDocument();
      });
    });

    it('shows failure analysis widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Failure Analysis')).toBeInTheDocument();
      });
    });

    it('translates camelCase failure-reason keys instead of rendering them raw (#1687 follow-up)', async () => {
      // The widget groups by the raw PrintLogEntry.failure_reason column.
      // The new editor stores camelCase keys (`filamentRunout`), so the widget
      // must translate them — otherwise users see the literal key text.
      server.use(
        http.get('/api/v1/archives/analysis/failures', () => {
          return HttpResponse.json({
            ...mockFailureAnalysis,
            failures_by_reason: { filamentRunout: 2, cloggedNozzle: 1 },
          });
        }),
      );

      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament runout')).toBeInTheDocument();
        expect(screen.getByText('Clogged nozzle')).toBeInTheDocument();
      });
      expect(screen.queryByText('filamentRunout')).not.toBeInTheDocument();
      expect(screen.queryByText('cloggedNozzle')).not.toBeInTheDocument();
    });

    it('renders legacy translated-text failure reasons unchanged (#1687 follow-up)', async () => {
      // Old rows from before the key/value migration stored the translated
      // text. The defaultValue fallback in the t() call must surface them
      // as-is rather than turning them into the literal key string.
      server.use(
        http.get('/api/v1/archives/analysis/failures', () => {
          return HttpResponse.json({
            ...mockFailureAnalysis,
            failures_by_reason: { 'Custom legacy reason': 4 },
          });
        }),
      );

      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Custom legacy reason')).toBeInTheDocument();
      });
    });

    it('shows printer stats widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Printer Stats')).toBeInTheDocument();
      });
    });

    it('shows filament trends widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Filament Trends')).toBeInTheDocument();
      });
    });

    it('shows records widget', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Records')).toBeInTheDocument();
      });
    });
  });

  describe('printer stats sub-cards', () => {
    it('shows prints by printer section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Prints by Printer')).toBeInTheDocument();
      });
    });

    it('shows print duration section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Duration')).toBeInTheDocument();
      });
    });

    it('shows print habits section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Habits')).toBeInTheDocument();
      });
    });

    it('shows print time of day section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Print Time of Day')).toBeInTheDocument();
      });
    });
  });

  describe('filament trends sub-cards', () => {
    it('shows by material section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('By Material')).toBeInTheDocument();
      });
    });

    it('shows success by material section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success by Material')).toBeInTheDocument();
      });
    });

    it('shows color distribution section', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Color Distribution')).toBeInTheDocument();
      });
    });
  });

  describe('records widget', () => {
    it('shows longest print record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Longest Print')).toBeInTheDocument();
      });
    });

    it('Longest Print excludes failed prints (#1390)', async () => {
      // After slim started populating actual_time_seconds for non-completed
      // rows (so Printer Stats By Time would match Quick Stats), a failed
      // print's elapsed duration could outrank successful prints in the
      // Records widget. RecordsWidget gates "Longest Print" on
      // status === 'completed' to preserve the pre-fix semantic.
      server.use(
        http.get('/api/v1/archives/slim', () =>
          HttpResponse.json([
            {
              id: 10, created_at: '2024-02-01T10:00:00Z',
              started_at: '2024-02-01T10:00:00Z', completed_at: null,
              print_name: 'Aborted 25h Marathon', status: 'failed',
              printer_id: 1, filament_type: 'PLA', filament_color: '#000000',
              filament_used_grams: 50, actual_time_seconds: 90000,
              print_time_seconds: 86400, cost: 1.50, quantity: 1,
            },
            {
              id: 11, created_at: '2024-02-02T10:00:00Z',
              started_at: '2024-02-02T10:00:00Z',
              completed_at: '2024-02-02T18:00:00Z',
              print_name: 'Successful 8h Print', status: 'completed',
              printer_id: 1, filament_type: 'PLA', filament_color: '#FF0000',
              filament_used_grams: 80, actual_time_seconds: 28800,
              print_time_seconds: 27000, cost: 2.40, quantity: 1,
            },
          ]),
        ),
      );
      render(<StatsPage />);

      // Wait for the records widget itself to render.
      await waitFor(() => {
        expect(screen.getByText('Longest Print')).toBeInTheDocument();
      });
      // The failed 25h print must not surface as any record — its presence
      // anywhere here would mean the status gate regressed.
      expect(screen.queryByText('Aborted 25h Marathon')).not.toBeInTheDocument();
      // The completed 8h print is the only candidate left, so it wins.
      expect(screen.getAllByText('Successful 8h Print').length).toBeGreaterThan(0);
    });

    it('shows heaviest print record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Heaviest Print')).toBeInTheDocument();
      });
    });

    it('shows most expensive record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Most Expensive')).toBeInTheDocument();
      });
    });

    it('shows success streak record', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Success Streak')).toBeInTheDocument();
      });
    });
  });

  describe('export', () => {
    it('has export button', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Export Stats')).toBeInTheDocument();
      });
    });
  });

  describe('recalculate costs', () => {
    it('has recalculate costs button', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Recalculate Costs')).toBeInTheDocument();
      });
    });
  });

  describe('user filter', () => {
    it('does not show user filter dropdown when auth is disabled', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Quick Stats')).toBeInTheDocument();
      });

      // Auth is disabled in our test setup (default), so user filter should not appear
      // The filter requires authEnabled && hasPermission('stats:filter_by_user')
      expect(screen.queryByText('All Users')).not.toBeInTheDocument();
    });
  });

  describe('energy warming-up indicator (#941)', () => {
    it('does not show a warning icon when energy data is available', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Energy Used')).toBeInTheDocument();
      });

      const energyLabel = screen.getByText('Energy Used').closest('div');
      expect(energyLabel?.querySelector('svg[aria-label]')).toBeNull();
    });

    it('shows a warning icon with tooltip next to energy stats when warming up', async () => {
      server.use(
        http.get('/api/v1/archives/stats', () => {
          return HttpResponse.json({ ...mockStats, energy_data_warming_up: true });
        })
      );

      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Energy Used')).toBeInTheDocument();
      });

      // Both Energy Used and Energy Cost labels get a warning icon with the
      // tooltip accessible via aria-label.
      const icons = await screen.findAllByLabelText(/still collecting hourly snapshots/i);
      expect(icons.length).toBe(2);
    });

    it('does not decorate other stats with the energy warming-up warning', async () => {
      server.use(
        http.get('/api/v1/archives/stats', () => {
          return HttpResponse.json({ ...mockStats, energy_data_warming_up: true });
        })
      );

      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Total Prints')).toBeInTheDocument();
      });

      const totalPrints = screen.getByText('Total Prints').closest('div');
      expect(totalPrints?.querySelector('svg[aria-label]')).toBeNull();
      const printTime = screen.getByText('Print Time').closest('div');
      expect(printTime?.querySelector('svg[aria-label]')).toBeNull();
    });
  });

  describe('the Prints / Fleet tab bar', () => {
    it('opens the Fleet tab from a ?tab=fleet deep link', async () => {
      window.history.replaceState({}, '', '/?tab=fleet');

      render(<StatsPage />);

      expect(
        await screen.findByRole('tab', { name: 'Fleet', selected: true }),
      ).toBeInTheDocument();
    });

    it('defaults to Prints when the URL says nothing', async () => {
      render(<StatsPage />);

      expect(
        await screen.findByRole('tab', { name: 'Prints', selected: true }),
      ).toBeInTheDocument();
    });

    it('writes the selection into the URL without adding a history entry', async () => {
      const pushState = vi.spyOn(window.history, 'pushState');
      render(<StatsPage />);

      await userEvent.click(await screen.findByRole('tab', { name: 'Fleet' }));

      await waitFor(() => expect(window.location.search).toBe('?tab=fleet'));
      expect(pushState).not.toHaveBeenCalled();
      pushState.mockRestore();
    });

    it('drops the param again on the way back to Prints', async () => {
      window.history.replaceState({}, '', '/?tab=fleet');
      render(<StatsPage />);

      await userEvent.click(await screen.findByRole('tab', { name: 'Prints' }));

      await waitFor(() => expect(window.location.search).toBe(''));
    });
  });

  describe('tab-aware header controls', () => {
    it('hides the archive controls on the Fleet tab', async () => {
      window.history.replaceState({}, '', '/?tab=fleet');
      render(<StatsPage />);
      await screen.findByRole('tab', { name: 'Fleet', selected: true });

      // All three act on print ARCHIVES, which the Fleet tab does not read.
      expect(screen.queryByText('Recalculate Costs')).not.toBeInTheDocument();
      expect(screen.queryByText('Export Stats')).not.toBeInTheDocument();
      expect(screen.queryByText('All Users')).not.toBeInTheDocument();
      // The layout and timeframe controls serve both tabs and stay.
      expect(screen.getByText('Reset Layout')).toBeInTheDocument();
    });

    it('keeps the archive controls on the Prints tab', async () => {
      render(<StatsPage />);

      await waitFor(() => {
        expect(screen.getByText('Recalculate Costs')).toBeInTheDocument();
      });
      expect(screen.getByText('Export Stats')).toBeInTheDocument();
    });

    it('resets only the active tab’s layout', async () => {
      window.history.replaceState({}, '', '/?tab=fleet');
      const removeItem = vi.mocked(localStorage.removeItem);
      removeItem.mockClear();

      render(<StatsPage />);
      await screen.findByRole('tab', { name: 'Fleet', selected: true });
      await userEvent.click(screen.getByText('Reset Layout'));

      expect(removeItem).toHaveBeenCalledWith(FLEET_DASHBOARD_STORAGE_KEY);
      expect(removeItem).not.toHaveBeenCalledWith(PRINTS_DASHBOARD_STORAGE_KEY);
    });

    it('resets the Prints layout when Prints is the active tab', async () => {
      const removeItem = vi.mocked(localStorage.removeItem);
      removeItem.mockClear();

      render(<StatsPage />);
      await waitFor(() => expect(screen.getByText('Reset Layout')).toBeInTheDocument());
      await userEvent.click(screen.getByText('Reset Layout'));

      expect(removeItem).toHaveBeenCalledWith(PRINTS_DASHBOARD_STORAGE_KEY);
      expect(removeItem).not.toHaveBeenCalledWith(FLEET_DASHBOARD_STORAGE_KEY);
    });
  });

  /**
   * The Fleet tab as a whole page — the three sections wired together, which is
   * the one thing no component test can see.
   *
   * Each section is proven on its own elsewhere (`FleetSummaryCard`,
   * `FleetMatrix`, `FleetWidgets`). What is only true HERE is the seam: that the
   * matrix and the grid render from the same loaded window the card does, that
   * they stay out of the way while that window is in flight or has failed, that
   * a keyboard drill-down works from the real page rather than from a component
   * mounted in isolation, and that the page's own Reset layout reaches the grid
   * the tab mounted.
   *
   * Copy is resolved through i18n, never typed, so a wording change moves the
   * test with it.
   */
  describe('the Fleet tab, end to end', () => {
    const t = (key: string, options?: Record<string, unknown>): string =>
      i18n.t(key, options as never) as string;

    /** The six grid widgets, in the order the wireframe lays them out. */
    const WIDGET_TITLES = (): string[] =>
      [
        'fleetMetrics.sections.stateOverTime',
        'fleetMetrics.sections.printsPerDay',
        'fleetMetrics.sections.downtimeByCause',
        'fleetMetrics.sections.coolingAndEject',
        'fleetMetrics.sections.recovery',
        'fleetMetrics.sections.partsBySku',
      ].map((key) => t(key));

    const summaryTable = (): HTMLElement => screen.getByRole('table', { name: t('fleetMetrics.sections.fleet') });
    const widgetTitles = (): (string | null)[] =>
      screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent);

    /** Wait until the history sweep has landed and the matrix is on screen. */
    const historyLanded = async (): Promise<HTMLElement> => screen.findByRole('grid');

    beforeEach(() => {
      window.history.replaceState({}, '', '/?tab=fleet');
      server.use(
        http.get('/api/v1/fleet-metrics/status', () => HttpResponse.json(makeFleetStatus())),
        http.get('/api/v1/fleet-metrics/overview', () => HttpResponse.json(makeFleetOverviewDay())),
        http.get('/api/v1/fleet-metrics/printers/:id/intervals', () =>
          HttpResponse.json(makePrinterIntervals()),
        ),
      );
    });

    it('lays the tab out summary → matrix → widgets, each under its own heading', async () => {
      render(<StatsPage />);
      await historyLanded();

      expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
        t('fleetMetrics.sections.fleet'),
        t('fleetMetrics.sections.matrix'),
      ]);
      expect(widgetTitles()).toEqual(WIDGET_TITLES());

      // …and in that document order, which the two queries above cannot state.
      const matrixHeading = screen.getByRole('heading', {
        level: 2,
        name: t('fleetMetrics.sections.matrix'),
      });
      const firstWidget = screen.getAllByRole('heading', { level: 3 })[0];
      expect(
        matrixHeading.compareDocumentPosition(firstWidget) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });

    it('answers "is anything down" while the window is still being swept', async () => {
      server.use(
        // Never settles: a year of history is still being classified.
        http.get('/api/v1/fleet-metrics/overview', () => new Promise<never>(() => {})),
      );

      render(<StatsPage />);

      // The live column is up…
      await waitFor(() => {
        const down = within(summaryTable()).getByRole('rowheader', { name: /^Down/ });
        expect(within(down.closest('tr') as HTMLElement).getAllByRole('cell')[0]).toHaveTextContent(
          '1',
        );
      });
      // …the wait is ANNOUNCED once rather than left as a blank page…
      const announcing = screen
        .getAllByRole('status')
        .filter((node) => node.textContent !== '')
        .map((node) => node.textContent);
      expect(announcing).toEqual([t('fleetMetrics.states.loading')]);
      // …and it is announced INSIDE the matrix section, which is always in the
      // flow. The section used to appear only once history landed, with a
      // reserved status row of its own above it; that row cost ~83 px of dead
      // space in the steady state to report a condition that is normally
      // absent, and its arrival moved the page.
      expect(
        screen.getByRole('heading', { level: 2, name: t('fleetMetrics.sections.matrix') }),
      ).toBeInTheDocument();
      // The grid itself and the widget grid still wait for real data.
      expect(screen.queryByRole('grid')).toBeNull();
      expect(screen.queryAllByRole('heading', { level: 3 })).toHaveLength(0);
    });

    it('brings both sections in when a failed sweep is retried', async () => {
      const user = userEvent.setup();
      let calls = 0;
      server.use(
        http.get('/api/v1/fleet-metrics/overview', () => {
          calls += 1;
          return calls === 1
            ? HttpResponse.json({ detail: 'boom' }, { status: 500 })
            : HttpResponse.json(makeFleetOverviewDay());
        }),
      );

      render(<StatsPage />);

      const retry = await screen.findByRole('button', { name: t('fleetMetrics.states.retry') });
      // A failed history sweep says nothing about now, so nothing below the
      // card is rendered and nothing above it is blanked.
      expect(screen.queryByRole('grid')).toBeNull();
      expect(summaryTable()).toBeInTheDocument();

      await user.click(retry);

      await historyLanded();
      expect(widgetTitles()).toEqual(WIDGET_TITLES());
    });

    it('drills down from the page with the keyboard alone, and comes back', async () => {
      const user = userEvent.setup();
      render(<StatsPage />);
      const grid = await historyLanded();

      // ONE tab stop for the whole grid, however many cells it holds.
      expect(grid.querySelectorAll('[tabindex="0"]')).toHaveLength(1);

      const start = grid.querySelector<HTMLElement>('[tabindex="0"]');
      expect(start).not.toBeNull();
      start!.focus();

      // Down into the first printer row, then right into a bucket column.
      await user.keyboard('{ArrowDown}{ArrowRight}{ArrowRight}{ArrowRight}{ArrowRight}');
      const cell = document.activeElement as HTMLElement;
      expect(grid.contains(cell)).toBe(true);

      await user.keyboard('{Enter}');
      expect(await screen.findByRole('dialog')).toBeInTheDocument();

      await user.keyboard('{Escape}');
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      // Back on the SAME cell — a drill-down that dumped focus at the top of
      // the page would make the grid unusable without a pointer.
      expect(document.activeElement).toBe(cell);
    });

    it('resets the Fleet grid to its defaults without touching the Prints layout', async () => {
      const user = userEvent.setup();
      const removeItem = vi.mocked(localStorage.removeItem);
      render(<StatsPage />);
      await historyLanded();

      const before = widgetTitles();
      expect(before).toHaveLength(6);

      // Hide the last widget through the grid's own control.
      const hideControls = screen.getAllByRole('button', { name: 'Hide widget' });
      expect(hideControls).toHaveLength(before.length);
      await user.click(hideControls[hideControls.length - 1]);
      await waitFor(() => expect(widgetTitles()).toHaveLength(before.length - 1));

      removeItem.mockClear();
      await user.click(screen.getByText('Reset Layout'));

      expect(removeItem).toHaveBeenCalledWith(FLEET_DASHBOARD_STORAGE_KEY);
      expect(removeItem).not.toHaveBeenCalledWith(PRINTS_DASHBOARD_STORAGE_KEY);
      // Clearing the key is not enough on its own: the grid reads its layout
      // once, at mount. The header bumps a remount key, and this is the only
      // place that whole mechanism is visible.
      await waitFor(() => expect(widgetTitles()).toEqual(before));
    });

    it('states the first-run situation once, and opens the matrix on Prints', async () => {
      server.use(
        http.get('/api/v1/fleet-metrics/status', () =>
          HttpResponse.json(makeFleetStatusFirstRun()),
        ),
        http.get('/api/v1/fleet-metrics/overview', () =>
          HttpResponse.json(makeFleetOverviewFirstRun()),
        ),
      );

      render(<StatsPage />);
      await historyLanded();

      expect(screen.getByRole('alert')).toBeInTheDocument();
      expect(screen.getByRole('tab', { selected: true, name: t('fleetMetrics.matrix.lens.prints') }))
        .toBeInTheDocument();
      expect(
        screen.getByRole('tab', { name: t('fleetMetrics.matrix.lens.timeSplit') }),
      ).toHaveAttribute('aria-disabled', 'true');
      // `recording_since` is null here, so the reason is the one that claims no
      // date rather than the one that names the window's own end.
      expect(
        screen.getByRole('button', { name: t('fleetMetrics.matrix.timeSplitDisabledNoData') }),
      ).toBeInTheDocument();
    });

    it('follows the bucket the SERVER echoed into the matrix column set', async () => {
      const columnNames = (grid: HTMLElement): string[] =>
        within(grid)
          .getAllByRole('columnheader')
          .map((header) => header.textContent ?? '');

      server.use(
        http.get('/api/v1/fleet-metrics/overview', () => HttpResponse.json(makeFleetOverviewWeek())),
      );
      const week = render(<StatsPage />);
      expect(columnNames(await historyLanded()).some((name) => /Week of/.test(name))).toBe(true);
      week.unmount();

      server.use(
        http.get('/api/v1/fleet-metrics/overview', () => HttpResponse.json(makeFleetOverviewHour())),
      );
      const hour = render(<StatsPage />);
      const hourNames = columnNames(await historyLanded());
      // An hour column names its own hour; a week column never could.
      expect(hourNames.some((name) => /\d{2}:\d{2}/.test(name))).toBe(true);
      expect(hourNames.some((name) => /Week of/.test(name))).toBe(false);
      hour.unmount();

      server.use(
        http.get('/api/v1/fleet-metrics/overview', () => HttpResponse.json(makeFleetOverviewDay())),
      );
      render(<StatsPage />);
      const dayNames = columnNames(await historyLanded());
      expect(dayNames.some((name) => /Week of/.test(name))).toBe(false);
      expect(dayNames.some((name) => /\d{2}:\d{2}/.test(name))).toBe(false);
    });
  });
});
