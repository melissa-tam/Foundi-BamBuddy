/**
 * Settings > Filament > Filament checks: "Default spool weight (g)". A stacked
 * SettingNumber (50-5000) under the tagless default filament editor; the
 * mechanism lives in its tooltip; a change round-trips through the save endpoint.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { SettingsPage } from '../../pages/SettingsPage';
import { server } from '../mocks/server';
import { setAuthToken } from '../../api/client';
import en from '../../i18n/locales/en';

const LABEL = en.settings.defaultSpoolWeight;
const HINT = en.settings.defaultSpoolWeightHint;

const mockSettings = {
  auto_archive: true,
  save_thumbnails: true,
  capture_finish_photo: true,
  default_filament_cost: 25.0,
  currency: 'USD',
  ams_humidity_good: 40,
  ams_humidity_fair: 60,
  ams_temp_good: 30,
  ams_temp_fair: 35,
  time_format: 'system',
  date_format: 'system',
  mqtt_enabled: false,
  spoolman_enabled: false,
  spoolman_url: '',
  ha_enabled: false,
  ha_url: '',
  ha_token: '',
  check_updates: false,
  check_printer_firmware: false,
  bed_cooled_threshold: 35,
  default_spool_weight_g: 1100,
};

describe('SettingsPage default spool weight', () => {
  const bodies: Array<Record<string, unknown>> = [];

  beforeEach(() => {
    window.history.replaceState({}, '', '/');
    setAuthToken(null);
    bodies.length = 0;
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json(mockSettings)),
      http.put('/api/v1/settings/', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        bodies.push(body);
        return HttpResponse.json({ ...mockSettings, ...body });
      }),
      http.get('/api/v1/printers/', () => HttpResponse.json([])),
      http.get('/api/v1/smart-plugs/', () => HttpResponse.json([])),
      http.get('/api/v1/notifications/', () => HttpResponse.json([])),
      http.get('/api/v1/api-keys/', () => HttpResponse.json([])),
      http.get('/api/v1/mqtt/status', () => HttpResponse.json({ enabled: false })),
      http.get('/api/v1/virtual-printer/status', () => HttpResponse.json({ running: false })),
      http.get('/api/v1/auth/status', () => HttpResponse.json({ auth_enabled: false, requires_setup: false })),
      http.get('/api/v1/external-links/', () => HttpResponse.json([])),
    );
  });

  const openFilamentTab = async (user: ReturnType<typeof userEvent.setup>) => {
    await waitFor(() => expect(screen.getAllByText('Filament').length).toBeGreaterThan(0));
    await user.click(screen.getAllByText('Filament')[0]);
  };

  it('shows the stored setting, bounded 50-5000, with the mechanism in a tooltip', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openFilamentTab(user);

    const field = (await screen.findByLabelText(LABEL)) as HTMLInputElement;
    expect(field.value).toBe('1100');
    expect(field).toHaveAttribute('min', '50');
    expect(field).toHaveAttribute('max', '5000');
    // One label per control; the explanation is not inline.
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: HINT }));
    expect(await screen.findByRole('tooltip')).toHaveTextContent(HINT);
  });

  it('saves a change, clamped to 5000', async () => {
    const user = userEvent.setup();
    render(<SettingsPage />);
    await openFilamentTab(user);

    const field = (await screen.findByLabelText(LABEL)) as HTMLInputElement;
    fireEvent.change(field, { target: { value: '750' } });
    await waitFor(() => expect(bodies.at(-1)?.default_spool_weight_g).toBe(750), { timeout: 5000 });

    fireEvent.change(field, { target: { value: '9000' } });
    expect(field.value).toBe('5000');
    await waitFor(() => expect(bodies.at(-1)?.default_spool_weight_g).toBe(5000), { timeout: 5000 });
  });
});
