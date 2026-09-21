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
  makeFleetStatus,
  makeFleetStatusFirstRun,
} from '../../fixtures/fleetMetrics';
import type { TimeframeState } from '../../../utils/timeframe';

const preset = (value: TimeframeState['preset']): TimeframeState => ({
  preset: value,
  dateFrom: undefined,
  dateTo: undefined,
});

/** The cells of the row whose row header matches, in column order. */
function rowCells(headerName: string | RegExp): HTMLElement[] {
  const header = screen.getByRole('rowheader', { name: headerName });
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
      expect(screen.getAllByRole('rowheader').map((header) => header.textContent)).toEqual([
        'Prints',
        'Prints per printer',
      ]);
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
