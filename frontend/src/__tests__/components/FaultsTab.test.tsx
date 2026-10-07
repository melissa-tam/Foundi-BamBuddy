/**
 * `components/incidents/FaultsTab` — the Stats page's fault ledger, driven
 * through MSW (`/fleet-metrics/status` for the site's today, `/incidents` for
 * the page). Asserted by role and label; copy is resolved through i18n, never
 * typed.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { FaultsTab } from '../../components/incidents/FaultsTab';
import i18n from '../../i18n';
import type { IncidentsResponse } from '../../types/incidents';
import type { TimeframeState } from '../../utils/timeframe';
import {
  RECURRING_PRINTER_ID,
  RECURRING_PRINTER_NAME,
  makeIncidentRow,
  makeIncidentsResponse,
  makeRecurringPrinterLine,
} from '../fixtures/incidents';
import { server } from '../mocks/server';
import { render } from '../utils';

const t = (key: string): string => String(i18n.t(key));

const LAST_30: TimeframeState = { preset: 'last-30', dateFrom: undefined, dateTo: undefined };

const PRINTERS = [
  { id: 10, name: '012-H2S', model: 'H2S', enabled: true },
  { id: RECURRING_PRINTER_ID, name: RECURRING_PRINTER_NAME, model: 'H2S', enabled: true },
];

/** Every `/incidents` request the tab made, in order. */
let requests: URLSearchParams[] = [];

function serve(response: IncidentsResponse | (() => Response)) {
  server.use(
    http.get('/api/v1/incidents', ({ request }) => {
      requests.push(new URL(request.url).searchParams);
      return typeof response === 'function' ? response() : HttpResponse.json(response);
    }),
  );
}

const lastRequest = (): URLSearchParams => requests[requests.length - 1]!;

const table = (): Promise<HTMLElement> => screen.findByRole('table', { name: t('fleetMetrics.tabs.faults') });

describe('FaultsTab', () => {
  beforeEach(() => {
    requests = [];
    server.use(http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)));
    serve(makeIncidentsResponse());
  });

  it('lists one table row per ledger row, newest page first', async () => {
    render(<FaultsTab timeframe={LAST_30} />);

    const rows = within(await table()).getAllByRole('row');
    // The header row plus the two items.
    expect(rows).toHaveLength(3);
    expect(lastRequest().get('date_from')).toBe('2026-08-23');
    expect(lastRequest().get('date_to')).toBe('2026-09-21');
    expect(lastRequest().get('limit')).toBe('50');
    expect(lastRequest().get('offset')).toBe('0');
  });

  it('re-requests with the chosen outcome', async () => {
    const user = userEvent.setup();
    render(<FaultsTab timeframe={LAST_30} />);
    await table();

    await user.selectOptions(screen.getByLabelText(t('incidents.filters.outcome')), 'held');

    await waitFor(() => expect(lastRequest().get('outcome')).toBe('held'));
  });

  it('pages on the server: Next asks for offset 50', async () => {
    const user = userEvent.setup();
    render(<FaultsTab timeframe={LAST_30} />);
    await table();

    await user.click(screen.getByRole('button', { name: t('common.pagination.next') }));

    await waitFor(() => expect(lastRequest().get('offset')).toBe('50'));
  });

  it('goes back to the first page when a filter changes', async () => {
    const user = userEvent.setup();
    render(<FaultsTab timeframe={LAST_30} />);
    await table();
    await user.click(screen.getByRole('button', { name: t('common.pagination.next') }));
    await waitFor(() => expect(lastRequest().get('offset')).toBe('50'));

    await user.selectOptions(screen.getByLabelText(t('incidents.filters.kind')), 'jam');

    await waitFor(() => {
      expect(lastRequest().get('kind')).toBe('jam');
      expect(lastRequest().get('offset')).toBe('0');
    });
  });

  it('states an empty window', async () => {
    serve(makeIncidentsResponse({ total: 0, items: [], recurring: [] }));
    render(<FaultsTab timeframe={LAST_30} />);

    expect(await screen.findByText(t('incidents.states.empty'))).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('reports a failed read and recovers on Retry', async () => {
    let calls = 0;
    serve(() => {
      calls += 1;
      return calls === 1
        ? HttpResponse.json({ detail: 'boom' }, { status: 500 })
        : HttpResponse.json(makeIncidentsResponse());
    });
    const user = userEvent.setup();
    render(<FaultsTab timeframe={LAST_30} />);

    const alert = await screen.findByRole('alert');
    await user.click(within(alert).getByRole('button', { name: t('incidents.states.retry') }));

    expect(await table()).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('renders no recurring strip when nothing recurs', async () => {
    serve(makeIncidentsResponse({ recurring: [] }));
    render(<FaultsTab timeframe={LAST_30} />);
    await table();

    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: t('incidents.recurring.heading') })).not.toBeInTheDocument();
  });

  it('lists one item per recurring line, under a heading carrying the rule tooltip', async () => {
    render(<FaultsTab timeframe={LAST_30} />);
    await table();

    const list = screen.getByRole('list');
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByRole('heading', { name: t('incidents.recurring.heading') })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t('incidents.recurring.hint') })).toBeInTheDocument();
  });

  it('narrows the table to a recurring line’s printer', async () => {
    const user = userEvent.setup();
    render(<FaultsTab timeframe={LAST_30} />);
    await table();

    const list = screen.getByRole('list');
    await user.click(within(list).getByRole('button', { name: RECURRING_PRINTER_NAME }));

    await waitFor(() => expect(lastRequest().get('printer_id')).toBe(String(RECURRING_PRINTER_ID)));
    expect(screen.getByLabelText(t('incidents.filters.printer'))).toHaveValue(String(RECURRING_PRINTER_ID));
  });

  it('opens each row on the site clock from the row’s own offset', async () => {
    serve(
      makeIncidentsResponse({
        total: 1,
        items: [makeIncidentRow({ created_at: '2026-10-07T10:18:21', utc_offset_minutes: -240 })],
      }),
    );
    render(<FaultsTab timeframe={LAST_30} />);

    const [, row] = within(await table()).getAllByRole('row');
    expect(within(row!).getByText(/06:18/)).toBeInTheDocument();
    expect(within(row!).queryByText(/10:18/)).not.toBeInTheDocument();
  });

  it('prints a code the catalog has no text for exactly once in its row', async () => {
    serve(
      makeIncidentsResponse({
        total: 1,
        items: [
          makeIncidentRow({
            code: '0700_0001',
            slot_desc: 'AMS A slot 3',
            printer_messages: [{ short_code: '0700_0001', description: '' }],
          }),
        ],
      }),
    );
    render(<FaultsTab timeframe={LAST_30} />);

    const [, row] = within(await table()).getAllByRole('row');
    // Matches both spellings, `0700_0001` and the formatted `0700-0001`.
    expect(within(row!).getAllByText(/0700.0001/)).toHaveLength(1);
  });

  it('names a recurring fault the catalog has no text for by its code alone', async () => {
    serve(
      makeIncidentsResponse({
        recurring: [
          makeRecurringPrinterLine({
            code: '0700_0012',
            printer_message: { short_code: '0700_0012', description: '' },
          }),
        ],
      }),
    );
    render(<FaultsTab timeframe={LAST_30} />);
    await table();

    const [line] = within(screen.getByRole('list')).getAllByRole('listitem');
    expect(within(line!).getByRole('button', { name: '0700_0012' })).toBeInTheDocument();
    expect(within(line!).getAllByText(/0700.0012/)).toHaveLength(1);
  });

  it('marks only the rows the server flagged as recurring', async () => {
    render(<FaultsTab timeframe={LAST_30} />);
    const [, plain, recurring] = within(await table()).getAllByRole('row');
    const marker = t('incidents.recurring.marker');

    expect(within(recurring!).getByText(marker)).toBeInTheDocument();
    expect(within(plain!).queryByText(marker)).not.toBeInTheDocument();
  });
});
