/**
 * FleetTab — the panel that wires the two fleet queries to the summary card.
 *
 * What is worth pinning here is the SEPARATION: the live column must survive a
 * history sweep that is slow, that fails, or that has nothing to report yet.
 * A tab that blanked "1 down" because a 366-day query returned 500 would be
 * hiding the only fact on the page worth acting on.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { FleetTab } from '../../../components/fleet/FleetTab';
import {
  FIXTURE_TZ_NAME,
  makeFleetOverviewDay,
  makeFleetOverviewFirstRun,
  makeFleetOverviewProduction,
  makeFleetStatus,
  makeFleetStatusFirstRun,
  makeFleetStatusProduction,
} from '../../fixtures/fleetMetrics';
import type { TimeframeState } from '../../../utils/timeframe';

const preset = (value: TimeframeState['preset']): TimeframeState => ({
  preset: value,
  dateFrom: undefined,
  dateTo: undefined,
});

/**
 * The summary card's own table.
 *
 * Scoped by name, because the tab now mounts the matrix under the card and the
 * matrix is a grid of row headers too — an unscoped `rowheader` query would
 * answer with every printer on the farm.
 */
const summaryTable = (): HTMLElement => screen.getByRole('table', { name: 'Fleet' });

/** The cells of the SUMMARY row whose row header matches, in column order. */
function rowCells(headerName: string | RegExp): HTMLElement[] {
  const header = within(summaryTable()).getByRole('rowheader', { name: headerName });
  const row = header.closest('tr');
  if (row === null) throw new Error('row header is not inside a row');
  return within(row).getAllByRole('cell');
}

describe('FleetTab', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/fleet-metrics/status', () => HttpResponse.json(makeFleetStatus())),
      http.get('/api/v1/fleet-metrics/overview', () => HttpResponse.json(makeFleetOverviewDay())),
    );
  });

  it('renders Now from /status while /overview is still in flight', async () => {
    server.use(
      // Never settles: history is still being swept.
      http.get('/api/v1/fleet-metrics/overview', () => new Promise<never>(() => {})),
    );

    render(<FleetTab timeframe={preset('last-30')} />);

    await waitFor(() => {
      expect(rowCells(/^Down/)[0]).toHaveTextContent('1');
    });
    // A live region announces its CONTENTS, so it has no accessible name to
    // query by; the toast region is a `status` too, and it is empty.
    const announcing = screen.getAllByRole('status').filter((el) => el.textContent !== '');
    expect(announcing).toHaveLength(1);
  });

  it('offers Retry on a failed history sweep and keeps Now live', async () => {
    server.use(
      http.get('/api/v1/fleet-metrics/overview', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    );

    render(<FleetTab timeframe={preset('last-30')} />);

    const retry = await screen.findByRole('button', { name: 'Retry' });
    expect(retry).toBeInTheDocument();
    expect(rowCells(/^Down/)[0]).toHaveTextContent('1');
  });

  it('re-asks for the window when Retry is pressed', async () => {
    let calls = 0;
    server.use(
      http.get('/api/v1/fleet-metrics/overview', () => {
        calls += 1;
        return HttpResponse.json({ detail: 'boom' }, { status: 500 });
      }),
    );

    render(<FleetTab timeframe={preset('last-30')} />);
    const retry = await screen.findByRole('button', { name: 'Retry' });
    const before = calls;

    await userEvent.click(retry);

    await waitFor(() => expect(calls).toBeGreaterThan(before));
  });

  it('states that recording has only just started, and drops the empty rows', async () => {
    server.use(
      http.get('/api/v1/fleet-metrics/status', () => HttpResponse.json(makeFleetStatusFirstRun())),
      http.get('/api/v1/fleet-metrics/overview', () =>
        HttpResponse.json(makeFleetOverviewFirstRun()),
      ),
    );

    render(<FleetTab timeframe={preset('all-time')} />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    await waitFor(() => {
      expect(
        within(summaryTable())
          .getAllByRole('rowheader')
          .map((header) => header.textContent),
      ).toEqual(['Prints']);
    });
  });

  describe('the recording notice', () => {
    /**
     * PRODUCTION, as of this wave: the recorder is an hour old and the window
     * is six weeks deep. The notice used to fire only when NOTHING had ever
     * been observed, so in the one state it was written for it said nothing.
     */
    it('fires when the window starts before the recorder did', async () => {
      server.use(
        http.get('/api/v1/fleet-metrics/status', () =>
          HttpResponse.json(makeFleetStatusProduction()),
        ),
        http.get('/api/v1/fleet-metrics/overview', () =>
          HttpResponse.json(makeFleetOverviewProduction()),
        ),
      );

      render(<FleetTab timeframe={preset('last-90')} />);

      const alert = await screen.findByRole('alert');
      // The dates are the SITE's, through the util's own formatters.
      expect(alert).toHaveTextContent('Sep 21, 2026');
      expect(alert).toHaveTextContent('Aug 8, 2026');
    });

    it('fires when nothing has ever been recorded', async () => {
      server.use(
        http.get('/api/v1/fleet-metrics/status', () => HttpResponse.json(makeFleetStatusFirstRun())),
      );

      render(<FleetTab timeframe={preset('last-30')} />);

      expect(await screen.findByRole('alert')).toBeInTheDocument();
    });

    it('stays silent when the window starts after the recorder did', async () => {
      // The default status records from 2026-09-01; "today" resolves to the
      // site's 21st, which is well inside recorded history.
      render(<FleetTab timeframe={preset('today')} />);

      await waitFor(() => expect(screen.getByRole('grid')).toBeInTheDocument());
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('holds the place of the summary card while /status is still in flight', async () => {
      server.use(
        http.get('/api/v1/fleet-metrics/status', () =>
          HttpResponse.json(makeFleetStatusProduction()),
        ),
      );

      const { container } = render(<FleetTab timeframe={preset('last-90')} />);

      // The notice's slot is the tab's first child BEFORE the answer lands and
      // the same element after, so the card below it never moves: only the
      // slot's contents arrive late.
      const slotBefore = container.firstElementChild?.firstElementChild;
      expect(slotBefore).not.toBeNull();
      expect(slotBefore?.children).toHaveLength(0);

      const alert = await screen.findByRole('alert');
      expect(slotBefore?.contains(alert)).toBe(true);
    });
  });

  it('resolves "all time" against the history the server states it has', async () => {
    let requested: URLSearchParams | null = null;
    server.use(
      http.get('/api/v1/fleet-metrics/overview', ({ request }) => {
        requested = new URL(request.url).searchParams;
        return HttpResponse.json(makeFleetOverviewDay());
      }),
    );

    render(<FleetTab timeframe={preset('all-time')} />);

    // `history_since` is 2026-08-02T11:30:00 and the resolver steps back a day
    // so a site zone ahead of UTC can never clip real history off the window.
    await waitFor(() => {
      expect(requested?.get('date_from')).toBe('2026-08-01');
    });
    expect(requested?.get('date_to')).toBe('2026-09-21');
  });

  it('states the resolved range, bucket and zone once history lands', async () => {
    render(<FleetTab timeframe={preset('last-30')} />);

    expect(await screen.findByText(new RegExp(FIXTURE_TZ_NAME))).toBeInTheDocument();
  });
});
