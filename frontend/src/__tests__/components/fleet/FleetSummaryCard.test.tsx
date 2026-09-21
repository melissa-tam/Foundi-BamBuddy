/**
 * FleetSummaryCard — the Fleet tab's hero.
 *
 * The card is a pure props component (its two queries live in `FleetTab`), so
 * these tests render it directly with fixture data and a router for the
 * printer links. What they pin is the card's contract rather than its markup:
 *
 * - Now fills from `/status` alone, with the period columns still empty.
 * - A change is announced as a SENTENCE, and "no previous window" is not
 *   announced as "no change".
 * - Down now is ordered actionable-first, capped, and every row is a link.
 * - The footer reports the AGE of the last good status, and crosses into a
 *   warning on a clock of its own — the one thing on this tab that must not
 *   depend on a refetch succeeding.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement } from 'react';
import i18n from '../../../i18n';
import { FleetSummaryCard } from '../../../components/fleet/FleetSummaryCard';
import type { FleetRange } from '../../../hooks/useFleetMetrics';
import {
  FIXTURE_TZ_NAME,
  makeFleetOverview,
  makeFleetOverviewFirstRun,
  makeFleetStatus,
} from '../../fixtures/fleetMetrics';
import type {
  FleetCause,
  FleetClassKey,
  FleetOverview,
  FleetStatus,
  PrinterStatus,
  SummaryRow,
} from '../../../types/fleetMetrics';
import type { TimeframeState } from '../../../utils/timeframe';

interface CardOptions {
  status?: FleetStatus | undefined;
  statusUpdatedAt?: number;
  statusError?: boolean;
  overview?: FleetOverview | undefined;
  preset?: TimeframeState['preset'];
  firstRun?: boolean;
  recordingGap?: boolean;
  range?: FleetRange | undefined;
}

function renderCard(options: CardOptions = {}) {
  const element: ReactElement = (
    <FleetSummaryCard
      status={'status' in options ? options.status : makeFleetStatus()}
      statusUpdatedAt={options.statusUpdatedAt ?? 0}
      statusError={options.statusError ?? false}
      overview={'overview' in options ? options.overview : makeFleetOverview()}
      preset={options.preset ?? 'last-30'}
      range={'range' in options ? options.range : undefined}
      firstRun={options.firstRun ?? false}
      recordingGap={options.recordingGap ?? false}
    />
  );
  return render(element, { wrapper: ({ children }) => <MemoryRouter>{children}</MemoryRouter> });
}

/** The cells of the row whose row header matches, in column order. */
function rowCells(headerName: string | RegExp): HTMLElement[] {
  const header = screen.getByRole('rowheader', { name: headerName });
  const row = header.closest('tr');
  if (row === null) throw new Error('row header is not inside a row');
  return within(row).getAllByRole('cell');
}

/** An overview carrying exactly the summary rows a test cares about. */
function overviewWithRows(rows: SummaryRow[]): FleetOverview {
  const base = makeFleetOverview();
  return { ...base, summary: { rows } };
}

function summaryRow(overrides: Partial<SummaryRow> & Pick<SummaryRow, 'key'>): SummaryRow {
  return { figure: null, previous: null, series: [], ...overrides };
}

const downPrinter = (
  printer_id: number,
  name: string,
  class_key: FleetClassKey,
  cause: FleetCause,
): PrinterStatus => ({
  printer_id,
  name,
  model: 'H2S',
  is_active: true,
  deleted: false,
  class_key,
  group: 'down',
  cause,
  since: '2026-09-20T20:04:00',
  since_open_ended: false,
});

afterEach(() => {
  vi.useRealTimers();
});

describe('FleetSummaryCard', () => {
  describe('the Now column', () => {
    it('renders live printer counts while the period columns are still empty', () => {
      // The whole reason the tab runs two queries: history has not answered,
      // and "is anything down right now" is already on screen.
      renderCard({ overview: undefined });

      expect(rowCells(/^Down/)[0]).toHaveTextContent('1');
      expect(rowCells('Printing')[0]).toHaveTextContent('1');
      // printers_known − out_of_fleet − not_recorded: 3 rows, one deactivated.
      expect(rowCells('In fleet')[0]).toHaveTextContent('2');
    });

    it('leaves Now blank on a rate row, which has no instantaneous value', () => {
      renderCard();

      expect(rowCells('Uptime')[0]).toHaveTextContent('');
    });

    it('renders the period figure beside it once history lands', () => {
      renderCard();

      // Both columns of a state row carry the same unit, so they compare.
      expect(rowCells(/^Down/)[1].textContent).toMatch(/\d/);
    });
  });

  describe('the change cell', () => {
    const upRow = summaryRow({ key: 'avg_down', figure: 2.1, previous: 1.7, series: [1, 2] });

    it('spells out a rise against the previous window', () => {
      renderCard({ overview: overviewWithRows([upRow]) });

      expect(
        screen.getByRole('img', { name: '0.4 more than the previous Last 30 days' }),
      ).toBeInTheDocument();
    });

    it('spells out a fall', () => {
      renderCard({
        overview: overviewWithRows([{ ...upRow, figure: 1.7, previous: 2.1 }]),
      });

      expect(
        screen.getByRole('img', { name: '0.4 less than the previous Last 30 days' }),
      ).toBeInTheDocument();
    });

    it('says "no change" when the two round to the same displayed figure', () => {
      renderCard({
        overview: overviewWithRows([{ ...upRow, figure: 2.14, previous: 2.13 }]),
      });

      expect(
        screen.getByRole('img', { name: 'No change from the previous Last 30 days' }),
      ).toBeInTheDocument();
    });

    it('distinguishes "no previous window" from "no change"', () => {
      renderCard({
        overview: overviewWithRows([{ ...upRow, figure: 2.1, previous: null }]),
      });

      // It names the MISSING COMPARISON, and names the period it would have
      // been against. A bare "No data" was ambiguous with the row's own figure
      // being absent, which is a different fact.
      expect(
        screen.getByRole('img', { name: 'No previous Last 30 days to compare with' }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('img', { name: /No change from the previous/ }),
      ).not.toBeInTheDocument();
    });

    it('states a ratio change in percentage points, not in percent of a percent', () => {
      renderCard({
        overview: overviewWithRows([
          summaryRow({ key: 'uptime', figure: 0.91, previous: 0.89, series: [0.9] }),
        ]),
      });

      expect(
        screen.getByRole('img', { name: '2 pt more than the previous Last 30 days' }),
      ).toBeInTheDocument();
    });
  });

  describe('Down now', () => {
    const problems: PrinterStatus[] = [
      downPrinter(1, 'A-fault', 'down:fault:jam', 'fault:jam'),
      downPrinter(2, 'B-offline', 'down:offline', 'offline'),
      downPrinter(3, 'C-plate', 'down:plate_held', 'plate_held'),
      downPrinter(4, 'D-usb', 'down:no_usb', 'no_usb'),
      downPrinter(5, 'E-paused', 'down:paused', 'paused'),
    ];

    it('lists the causes somebody standing there can fix first, capped at three', () => {
      renderCard({ status: makeFleetStatus({ printers: problems }) });

      const links = screen.getAllByRole('link');
      expect(links.map((link) => link.textContent)).toEqual(['C-plate', 'D-usb', 'E-paused']);
    });

    it('states how many more are down rather than dropping them', () => {
      renderCard({ status: makeFleetStatus({ printers: problems }) });

      expect(screen.getByText(/\+2/)).toBeInTheDocument();
    });

    it('links each row to its printer', () => {
      renderCard({ status: makeFleetStatus({ printers: problems }) });

      expect(screen.getByRole('link', { name: 'C-plate' })).toHaveAttribute(
        'href',
        '/?printer=3',
      );
    });

    it('renders no links when nothing is down', () => {
      const healthy = makeFleetStatus().printers.filter((printer) => printer.group !== 'down');
      renderCard({ status: makeFleetStatus({ printers: healthy }) });

      expect(screen.queryAllByRole('link')).toHaveLength(0);
    });
  });

  describe('the footer', () => {
    it('warns once the last good status passes 90 seconds, on its own clock', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-09-21T12:00:00Z'));
      renderCard({ statusUpdatedAt: Date.now() });

      expect(screen.queryByLabelText('Warning')).not.toBeInTheDocument();

      // No refetch, no new props: the age has to advance by itself, because a
      // stopped poll is exactly the case this surface exists to report.
      act(() => {
        vi.advanceTimersByTime(90_000);
      });

      expect(screen.getByLabelText('Warning')).toBeInTheDocument();
    });

    it('warns immediately when the status query is failing', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-09-21T12:00:00Z'));
      renderCard({ statusUpdatedAt: Date.now(), statusError: true });

      expect(screen.getByLabelText('Warning')).toBeInTheDocument();
    });

    it('states nothing before the first status has landed', () => {
      renderCard({ statusUpdatedAt: 0 });

      expect(screen.queryByLabelText('Warning')).not.toBeInTheDocument();
    });
  });

  describe('the header', () => {
    it('states the resolved range, bucket and zone', () => {
      renderCard();

      expect(screen.getByText(new RegExp(FIXTURE_TZ_NAME))).toBeInTheDocument();
    });

    it('states no range until history lands', () => {
      renderCard({ overview: undefined });

      expect(screen.queryByText(new RegExp(FIXTURE_TZ_NAME))).not.toBeInTheDocument();
    });
  });

  it('drops the rows it has nothing to say about on a first run', () => {
    renderCard({ overview: makeFleetOverviewFirstRun(), firstRun: true });

    // Only the print-log rate survives. Print history is complete for its own
    // past, so "Prints / day" is a real figure; "Prints per printer / day"
    // divides by COUNTED printer-days, which only a recorded bucket produces,
    // so on a first run it has no denominator at all.
    const headers = screen.getAllByRole('rowheader');
    expect(headers.map((header) => header.textContent)).toEqual(['Prints']);
  });

  describe('the rate rows carry their unit', () => {
    it('reads "/ day" beside both rates, and beside nothing else', () => {
      // Production read "Prints 21" next to a matrix total of 900 — a per-day
      // average with no unit on it.
      renderCard();

      const perDay = i18n.t('fleetMetrics.units.perDay');
      expect(rowCells('Prints')[1].textContent).toContain(perDay);
      expect(rowCells('Prints per printer')[1].textContent).toContain(perDay);
      expect(rowCells(/^Down/)[1].textContent).not.toContain(perDay);
      expect(rowCells('Uptime')[1].textContent).not.toContain(perDay);
    });

    it('states no unit where there is no figure to qualify', () => {
      renderCard({ overview: undefined });

      expect(rowCells('Prints')[1].textContent).not.toContain(
        i18n.t('fleetMetrics.units.perDay'),
      );
    });
  });

  describe('the window qualifier on the hints', () => {
    const hintNames = (): string[] =>
      screen.getAllByRole('button').map((node) => node.getAttribute('aria-label') ?? '');

    it('says the period figures cover recorded time only when the window outruns the recorder', () => {
      renderCard({ recordingGap: true });

      const qualifier = i18n.t('fleetMetrics.hints.whileRecorded');
      expect(hintNames().some((name) => name.includes(qualifier))).toBe(true);
      // It joins the row's OWN sentence rather than arriving as a second
      // trigger beside it.
      const uptime = i18n.t('fleetMetrics.hints.uptime');
      expect(hintNames().some((name) => name.includes(uptime) && name.includes(qualifier))).toBe(
        true,
      );
    });

    it('leaves the hints alone when the recorder covered the whole window', () => {
      renderCard({ recordingGap: false });

      expect(
        hintNames().some((name) => name.includes(i18n.t('fleetMetrics.hints.whileRecorded'))),
      ).toBe(false);
    });
  });

  describe('a one-day window', () => {
    it('names the single date once rather than either side of a dash', () => {
      const overview = makeFleetOverview();
      overview.date_from = overview.date_to;
      renderCard({ overview });

      const header = screen.getByTitle(new RegExp(FIXTURE_TZ_NAME));
      expect(header.textContent).toContain('Sep 21, 2026');
      expect(header.textContent?.match(/Sep 21, 2026/g)).toHaveLength(1);
    });

    it('does the same before history has landed', () => {
      renderCard({
        overview: undefined,
        range: { dateFrom: '2026-09-21', dateTo: '2026-09-21' },
      });

      const header = screen.getByTitle(new RegExp(FIXTURE_TZ_NAME));
      expect(header.textContent?.match(/Sep 21, 2026/g)).toHaveLength(1);
    });
  });
});
