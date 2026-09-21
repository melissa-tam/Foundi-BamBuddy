/**
 * BucketDetail — the drill-down behind a matrix cell.
 *
 * The whole file runs with the process zone set to **Pacific/Kiritimati
 * (UTC+14)** while the fixtures' site is UTC+12. Every rendered timestamp is
 * therefore two hours away from what a browser-zone formatter would produce,
 * which is the only way to prove the dialog reads the SITE's clock. The first
 * test asserts the hostile zone actually took, so a silently ignored `TZ`
 * cannot let the rest pass for the wrong reason.
 */
process.env.TZ = 'Pacific/Kiritimati';

import { describe, it, expect, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render, screen, waitFor, within } from '../../utils';
import { server } from '../../mocks/server';
import i18n from '../../../i18n';
import { BucketDetail } from '../../../components/fleet/BucketDetail';
import {
  PROBLEM_PRINTER_ID,
  makeFleetOverview,
  makePrinterIntervals,
} from '../../fixtures/fleetMetrics';
import type {
  ClassifiedInterval,
  FleetBucket,
  PrinterIntervalsResponse,
} from '../../../types/fleetMetrics';

const INTERVALS_PATH = '/api/v1/fleet-metrics/printers/:id/intervals';

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options as never) as string;

/** A naive-UTC instant (no `Z`), the shape every timestamp on the wire has. */
const naive = (ms: number): string => new Date(ms).toISOString().slice(0, 19);

interface RenderOptions {
  bucketWidth?: FleetBucket;
  /** Which bucket of the window to open. Defaults to the last complete one. */
  bucketIndex?: number;
}

function renderDetail({ bucketWidth = 'day', bucketIndex }: RenderOptions = {}) {
  const overview = makeFleetOverview({ bucket: bucketWidth });
  const buckets = overview.matrix.series.buckets;
  const index = bucketIndex ?? buckets.length - 2;
  const seriesBucket = buckets[index]!;
  const printer = overview.matrix.printers.find((row) => row.printer_id === PROBLEM_PRINTER_ID)!;

  const view = render(
    <BucketDetail
      printer={printer}
      cell={seriesBucket.values.printers[String(PROBLEM_PRINTER_ID)]}
      seriesBucket={seriesBucket}
      bucketWidth={overview.bucket}
      tzName={overview.tz_name}
      onClose={() => {}}
    />,
  );
  return { ...view, seriesBucket, printer, overview };
}

/** One named section of the dialog. Every section is labelled by its heading. */
function region(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

/** The intervals query has answered: the two on-demand sections are rendered. */
async function loaded(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: i18n.t('fleetMetrics.detail.intervals') as string });
}

/** Swap in a handler that records the request URL and answers with `body`. */
function captureIntervals(body?: Partial<PrinterIntervalsResponse>): { calls: URL[] } {
  const calls: URL[] = [];
  server.use(
    http.get(INTERVALS_PATH, ({ request }) => {
      calls.push(new URL(request.url));
      return HttpResponse.json(makePrinterIntervals(body));
    }),
  );
  return { calls };
}

describe('BucketDetail', () => {
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    user = userEvent.setup();
  });

  it('runs against a browser zone that is NOT the site zone', () => {
    expect(new Date().getTimezoneOffset()).toBe(-840);
  });

  it('names the dialog after the printer, the bucket and the zone', async () => {
    const { seriesBucket, overview } = renderDetail();
    const dialog = screen.getByRole('dialog');

    expect(dialog).toHaveAccessibleName(expect.stringContaining('009-H2C'));
    expect(within(dialog).getByText(overview.tz_name)).toBeInTheDocument();
    expect(seriesBucket.utc_offset_minutes).toBe(720);
    await waitFor(() => expect(screen.getByRole('link')).toBeInTheDocument());
  });

  it('renders the class split and the prints by outcome from the cell it was opened with', () => {
    renderDetail();

    // The problem printer's fixture bucket carries printing, cooling, a jam
    // and a held plate.
    const classes = region(t('fleetMetrics.detail.classes'));
    expect(within(classes).getByText(t('printers.status.printing'))).toBeInTheDocument();
    expect(within(classes).getByText(t('fleetMetrics.class.cooling'))).toBeInTheDocument();
    expect(within(classes).getByText(t('printers.incident.jam'))).toBeInTheDocument();

    const prints = region(t('fleetMetrics.detail.printsByOutcome'));
    expect(within(prints).getByText(t('fleetMetrics.detail.outcome.completed'))).toBeInTheDocument();
  });

  it('renders the intervals and incidents the API answers with', async () => {
    renderDetail();
    await loaded();

    // Five classified intervals in the fixture, one incident.
    expect(within(region(t('fleetMetrics.detail.intervals'))).getAllByRole('listitem')).toHaveLength(5);
    expect(within(region(t('fleetMetrics.detail.incidents'))).getAllByRole('listitem')).toHaveLength(1);
  });

  it('renders interval times in the SITE zone, not the browser zone', async () => {
    renderDetail();

    // 2026-09-18T12:00:00 naive UTC + 12 h = Sep 19, 00:00 at the site.
    // A browser-zone render under UTC+14 would read Sep 19, 02:00.
    expect(await screen.findByText(/Sep 19, 00:00/)).toBeInTheDocument();
    expect(screen.queryByText(/Sep 19, 02:00/)).not.toBeInTheDocument();
  });

  it('calls an open incident ongoing', async () => {
    captureIntervals({
      incidents: [{ incident_id: 7, kind: 'jam', created_at: '2026-09-18T19:03:25', resolved_at: null }],
    });
    renderDetail();

    expect(await screen.findByText(new RegExp(t('fleetMetrics.detail.ongoing')))).toBeInTheDocument();
  });

  it('asks for exactly the bucket day on a day bucket', async () => {
    const captured = captureIntervals();
    const { seriesBucket } = renderDetail();

    await waitFor(() => expect(captured.calls).toHaveLength(1));
    const params = captured.calls[0]!.searchParams;
    expect(params.get('date_from')).toBe(siteDate(seriesBucket.start));
    expect(params.get('date_to')).toBe(siteDate(seriesBucket.start));
  });

  it('asks for seven days on a week bucket', async () => {
    const captured = captureIntervals();
    const { seriesBucket } = renderDetail({ bucketWidth: 'week' });

    await waitFor(() => expect(captured.calls).toHaveLength(1));
    const params = captured.calls[0]!.searchParams;
    const from = params.get('date_from')!;
    expect(from).toBe(siteDate(seriesBucket.start));
    expect(daysBetween(from, params.get('date_to')!)).toBe(6);
  });

  it('asks for one day on an hour bucket and shows only the intersecting intervals', async () => {
    const overview = makeFleetOverview({ bucket: 'hour' });
    const seriesBucket = overview.matrix.series.buckets[5]!;
    const startMs = Date.parse(`${seriesBucket.start}Z`);
    const inside: ClassifiedInterval = {
      start: naive(startMs + 600_000),
      end: naive(startMs + 1_800_000),
      class_key: 'printing',
      group: 'printing',
      cause: null,
      seconds: 1200,
    };
    const before: ClassifiedInterval = {
      start: naive(startMs - 7_200_000),
      end: naive(startMs - 3_600_000),
      class_key: 'idle',
      group: 'idle',
      cause: null,
      seconds: 3600,
    };
    const captured = captureIntervals({ intervals: [before, inside], incidents: [] });

    const printer = overview.matrix.printers.find((row) => row.printer_id === PROBLEM_PRINTER_ID)!;
    render(
      <BucketDetail
        printer={printer}
        cell={seriesBucket.values.printers[String(PROBLEM_PRINTER_ID)]}
        seriesBucket={seriesBucket}
        bucketWidth="hour"
        tzName={overview.tz_name}
        onClose={() => {}}
      />,
    );

    await waitFor(() => expect(captured.calls).toHaveLength(1));
    const params = captured.calls[0]!.searchParams;
    expect(params.get('date_from')).toBe(params.get('date_to'));
    expect(params.get('date_from')).toBe(siteDate(seriesBucket.start));

    await loaded();
    const list = region(t('fleetMetrics.detail.intervals'));
    // The interval two hours before the bucket is filtered out.
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
    expect(within(list).getByText(t('printers.status.printing'), { exact: false })).toBeInTheDocument();
  });

  it('states the failure and retries on request', async () => {
    let attempts = 0;
    server.use(
      http.get(INTERVALS_PATH, () => {
        attempts += 1;
        return attempts === 1
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json(makePrinterIntervals());
      }),
    );
    renderDetail();

    expect(await screen.findByRole('alert')).toHaveTextContent(t('fleetMetrics.states.loadFailed'));
    expect(screen.queryByRole('region', { name: t('fleetMetrics.detail.intervals') })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: t('fleetMetrics.states.retry') }));

    await loaded();
    expect(attempts).toBe(2);
  });

  it('says so when the range holds no intervals and no incidents', async () => {
    captureIntervals({ intervals: [], incidents: [] });
    renderDetail();

    expect(await screen.findByText(t('fleetMetrics.detail.noIntervals'))).toBeInTheDocument();
    expect(screen.getByText(t('fleetMetrics.detail.noIncidents'))).toBeInTheDocument();
  });
});

/** The site-local calendar date of a naive-UTC instant at the fixtures' +12. */
function siteDate(naiveUtc: string): string {
  return new Date(Date.parse(`${naiveUtc}Z`) + 720 * 60_000).toISOString().slice(0, 10);
}

function daysBetween(from: string, to: string): number {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);
}
