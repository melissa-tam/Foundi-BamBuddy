/**
 * ShopAirReadout — the first row of Settings → Farm → "Eject cooldown": the
 * measured shop air and the eject line, as two labelled values.
 *
 * Pinned: each value is named by its own caption (a `definition` role query by
 * name), the three server bases render their values and carry HOW the value is
 * known in the InfoHint tooltip — never inline — and an unknown basis says so in
 * the value slots ("no reading" / "at plateau") instead of inventing a number.
 * Copy is resolved through the live i18n instance, so a wording edit never
 * breaks these tests; the numbers are data.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import i18n from '../../../i18n';
import { ShopAirReadout } from '../../../components/settings/ShopAirReadout';
import { formatTimeOnly } from '../../../utils/date';
import type { ShopAirResponse } from '../../../api/client';

// A fixed local clock: the "N min ago" and the carried time are derived from it.
const NOW = new Date(2026, 8, 25, 15, 0, 0);

function serve(body: ShopAirResponse) {
  server.use(http.get('/api/v1/shop-air', () => HttpResponse.json(body)));
}

const shopAir = () => screen.getByRole('definition', { name: i18n.t('settings.shopAir.label') });
const ejectLine = () =>
  screen.getByRole('definition', { name: i18n.t('settings.shopAir.ejectLine') });

describe('ShopAirReadout', () => {
  beforeEach(() => {
    // Date only: TanStack Query and user-event keep their real timers.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows the fresh shop air and eject line, with the measurement in the tooltip', async () => {
    serve({
      value_c: 25.8,
      as_of: new Date(NOW.getTime() - 14 * 60_000).toISOString(),
      basis: 'fresh',
      printers: 3,
      margin_c: 2,
      eject_line_c: 27.8,
    });
    const user = userEvent.setup();
    render(<ShopAirReadout />);

    await waitFor(() => expect(shopAir()).toHaveTextContent('25.8 °C'));
    expect(ejectLine()).toHaveTextContent('27.8 °C');

    const basis = i18n.t('settings.shopAir.fresh', { count: 3, minutes: 14 });
    // Supplementary: never inline, only in the tooltip.
    expect(screen.queryByText(basis)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: basis }));
    expect(await screen.findByRole('tooltip')).toHaveTextContent(basis);
  });

  it('names the carried sample time (today) in the tooltip', async () => {
    const carriedAt = new Date(2026, 8, 25, 4, 10);
    serve({
      value_c: 22.4,
      as_of: carriedAt.toISOString(),
      basis: 'carried',
      printers: 1,
      margin_c: 2,
      eject_line_c: 24.4,
    });
    render(<ShopAirReadout timeFormat="24h" />);

    await waitFor(() => expect(shopAir()).toHaveTextContent('22.4 °C'));
    expect(ejectLine()).toHaveTextContent('24.4 °C');
    const basis = i18n.t('settings.shopAir.carried', { time: formatTimeOnly(carriedAt, '24h') });
    expect(screen.getByRole('button', { name: basis })).toBeInTheDocument();
  });

  it('adds the date when the carried sample is from an earlier day', async () => {
    const carriedAt = new Date(2026, 8, 23, 22, 5);
    serve({
      value_c: 23.1,
      as_of: carriedAt.toISOString(),
      basis: 'carried',
      printers: 1,
      margin_c: 2,
      eject_line_c: 25.1,
    });
    render(<ShopAirReadout />);

    await waitFor(() => expect(shopAir()).toHaveTextContent('23.1 °C'));
    const day = carriedAt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const hint = screen.getAllByRole('button').find((b) =>
      (b.getAttribute('aria-label') ?? '').includes(day),
    );
    expect(hint).toBeDefined();
  });

  it('shows no reading and the plateau when shop air is unknown', async () => {
    serve({
      value_c: null,
      as_of: null,
      basis: 'unknown',
      printers: 0,
      margin_c: 2,
      eject_line_c: null,
    });
    render(<ShopAirReadout />);

    await waitFor(() => expect(shopAir()).toHaveTextContent(i18n.t('settings.shopAir.noReading')));
    expect(ejectLine()).toHaveTextContent(i18n.t('settings.shopAir.atPlateau'));
    // No number is invented for either slot.
    expect(shopAir().textContent).not.toMatch(/\d/);
    expect(ejectLine().textContent).not.toMatch(/\d/);
    expect(
      screen.getByRole('button', { name: i18n.t('settings.shopAir.unknown') }),
    ).toBeInTheDocument();
  });

  it('states a failed read in the tooltip and shows no values', async () => {
    server.use(http.get('/api/v1/shop-air', () => new HttpResponse(null, { status: 500 })));
    render(<ShopAirReadout />);

    expect(
      await screen.findByRole('button', { name: i18n.t('settings.shopAir.readFailed') }),
    ).toBeInTheDocument();
    expect(shopAir().textContent).not.toMatch(/\d/);
    expect(ejectLine().textContent).not.toMatch(/\d/);
  });
});
