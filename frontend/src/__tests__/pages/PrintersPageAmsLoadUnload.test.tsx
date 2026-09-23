/**
 * Tests for the AMS load / unload verbs on PrintersPage (#891).
 *
 * The printer-card refactor in #1661 replaced the kebab "Slot options"
 * button with a hover card (FilamentHoverCard) whose actions render on
 * hover. Tests `fireEvent.mouseEnter` on the slot trigger
 * (`data-testid="filament-slot"`) and wait for the portaled card to
 * appear in document.body before clicking Load.
 *
 * Unload is NOT a slot verb: `/ams/unload` takes no tray argument, so it lives
 * once in each AMS unit's header (B4 slot-verb consolidation) and needs no hover.
 *
 * A 200 answers `{ outcome, message }`; the toast is keyed off `outcome` and the
 * backend `message` is never rendered. `showToast` is spied (and still forwarded
 * to the real provider) so each case pins both the rendered copy and the variant.
 *
 * While a recovery driver is live (`open_incident.driver_live`) a Load/Unload click
 * asks first — sending the command ends the driver. The gate is `driver_live` alone:
 * a `recovering` row without a live driver, or no incident, sends at once.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { AmsCommandOutcome, OpenIncidentState } from '../../api/client';
import type { ToastType } from '../../contexts/ToastContext';
import en from '../../i18n/locales/en';

const { showToastSpy } = vi.hoisted(() => ({ showToastSpy: vi.fn() }));

/**
 * Record every showToast call, then forward it so the real ToastProvider still
 * renders the toast. The wrapper is cached per provider function so its identity
 * is as stable as the provider's own useCallback.
 */
vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  type ShowToast = ReturnType<typeof actual.useToast>['showToast'];
  const spied = new WeakMap<ShowToast, ShowToast>();
  return {
    ...actual,
    useToast: () => {
      const context = actual.useToast();
      let showToast = spied.get(context.showToast);
      if (!showToast) {
        const original = context.showToast;
        showToast = (message, type) => {
          showToastSpy(message, type);
          original(message, type);
        };
        spied.set(original, showToast);
      }
      return { ...context, showToast };
    },
  };
});

const mockPrinter = {
  id: 1,
  name: 'X1 Carbon',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  nozzle_diameter: 0.4,
  nozzle_type: 'hardened_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const baseTray = {
  tray_color: 'FF0000FF',
  tray_type: 'PLA',
  tray_sub_brands: 'PLA Basic',
  tray_id_name: 'A00-R0',
  tray_info_idx: 'GFA00',
  remain: 80,
  k: 0.02,
  cali_idx: null,
  tag_uid: null,
  tray_uuid: null,
  nozzle_temp_min: 190,
  nozzle_temp_max: 230,
  drying_temp: null,
  drying_time: null,
  state: 11,
};

const mockIdleStatusWithAms = {
  connected: true,
  state: 'IDLE',
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  speed_level: 2,
  vt_tray: [],
  ams: [
    {
      id: 0,
      humidity: 30,
      temp: 25,
      is_ams_ht: false,
      serial_number: 'AMS00',
      sw_ver: '1.0.0',
      dry_time: 0,
      dry_status: 0,
      dry_sub_status: 0,
      dry_sf_reason: [],
      module_type: 'ams',
      tray: [
        { id: 0, ...baseTray },
        { id: 1, ...baseTray, tray_color: '00FF00FF', tray_type: 'PETG' },
        { id: 2, ...baseTray, tray_color: '0000FFFF', tray_type: 'ABS' },
        { id: 3, ...baseTray, tray_color: 'FFFF00FF', tray_type: 'TPU' },
      ],
    },
  ],
};

const mockRunningStatus = {
  ...mockIdleStatusWithAms,
  state: 'RUNNING',
};

/** Hover-card visibility flips after an 80ms timeout — wait it out. */
async function hoverSlot(slot: Element) {
  fireEvent.mouseEnter(slot);
  await waitFor(() => {
    expect(screen.getByText('Load')).toBeInTheDocument();
  });
}

const toastCopy = en.printers.toast;

/** [outcome, rendered copy, toast variant] for a Load click. */
const loadOutcomes: Array<[AmsCommandOutcome, string, ToastType]> = [
  ['complete', toastCopy.loadInitiated, 'success'],
  ['acted', toastCopy.loadInitiated, 'success'],
  ['no_movement', toastCopy.amsLoadNoMovement, 'warning'],
  ['undecidable', toastCopy.amsUnloadNothingLoaded, 'info'],
  ['session_changed', toastCopy.amsCommandSessionChanged, 'warning'],
  ['held', toastCopy.amsLoadHeld, 'warning'],
];

/** [outcome, rendered copy, toast variant] for an Unload click. */
const unloadOutcomes: Array<[AmsCommandOutcome, string, ToastType]> = [
  ['complete', toastCopy.unloadInitiated, 'success'],
  ['acted', toastCopy.unloadInitiated, 'success'],
  ['no_movement', toastCopy.amsUnloadNoMovement, 'warning'],
  ['undecidable', toastCopy.amsUnloadNothingLoaded, 'info'],
  ['session_changed', toastCopy.amsCommandSessionChanged, 'warning'],
  ['held', toastCopy.amsUnloadHeld, 'warning'],
];

/** A backend sentence that must never reach the screen. */
const BACKEND_MESSAGE = 'backend fallback sentence';

describe('PrintersPage - AMS load/unload (#891)', () => {
  beforeEach(() => {
    showToastSpy.mockClear();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
    );
  });

  it('Load posts to /ams/load with tray_id derived from amsId*4 + slot', async () => {
    const user = userEvent.setup();
    let captured: { tray_id: string | null } | null = null;

    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockIdleStatusWithAms)),
      http.post('/api/v1/printers/:id/ams/load', ({ request }) => {
        const url = new URL(request.url);
        captured = { tray_id: url.searchParams.get('tray_id') };
        return HttpResponse.json({ outcome: 'acted', message: 'Loading filament from AMS 0 slot 3' });
      }),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    // Slot 2 (third one, slotIdx=2) → expected tray_id = 0*4 + 2 = 2
    const slots = screen.getAllByTestId('filament-slot');
    await hoverSlot(slots[2]);
    await user.click(screen.getByText('Load'));

    await waitFor(() => {
      expect(captured).not.toBeNull();
      expect(captured!.tray_id).toBe('2');
    });
  });

  it('Unload renders ONCE per AMS unit and posts to /ams/unload (no body, no params)', async () => {
    const user = userEvent.setup();
    let unloadCalled = false;

    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockIdleStatusWithAms)),
      http.post('/api/v1/printers/:id/ams/unload', () => {
        unloadCalled = true;
        return HttpResponse.json({ outcome: 'acted', message: 'Unloading filament' });
      }),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    // One AMS unit with four slots — one Unload button, in the unit header. Four
    // copies of a printer-level command (one per slot) implied a per-slot unload
    // the wire cannot express.
    const unloadButtons = screen.getAllByRole('button', { name: 'Unload' });
    expect(unloadButtons).toHaveLength(1);

    await user.click(unloadButtons[0]);

    await waitFor(() => {
      expect(unloadCalled).toBe(true);
    });
  });

  it('does not offer Unload inside a slot hover card', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockIdleStatusWithAms)),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    const slots = screen.getAllByTestId('filament-slot');
    await hoverSlot(slots[0]);

    // The card is open (Load is in it) and still only the header's single Unload
    // exists — the hover card contributed none.
    expect(screen.getByText('Load')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Unload' })).toHaveLength(1);
  });

  it('disables Load / Unload while the printer is RUNNING', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockRunningStatus)),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    // Hover to reveal the actions — they should be present but disabled
    // while the printer is running (replaces the pre-#1661 behavior where
    // the trigger button was hidden entirely).
    const slots = screen.getAllByTestId('filament-slot');
    fireEvent.mouseEnter(slots[0]);

    await waitFor(() => {
      expect(screen.getByText('Load')).toBeInTheDocument();
    });

    expect(screen.getByText('Load').closest('button')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Unload' })).toBeDisabled();
  });

  it('external spool slot exposes Load and posts tray_id=254', async () => {
    const user = userEvent.setup();
    let captured: string | null = null;

    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({
          ...mockIdleStatusWithAms,
          ams: [], // external-only
          vt_tray: [{ id: 254, ...baseTray, tray_type: 'PLA', tray_color: 'FFFFFFFF' }],
        }),
      ),
      http.post('/api/v1/printers/:id/ams/load', ({ request }) => {
        captured = new URL(request.url).searchParams.get('tray_id');
        return HttpResponse.json({ outcome: 'acted', message: 'Loading filament from external spool' });
      }),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    const slots = screen.getAllByTestId('filament-slot');
    await hoverSlot(slots[0]);
    await user.click(screen.getByText('Load'));

    await waitFor(() => {
      expect(captured).toBe('254');
    });
  });

  it.each(loadOutcomes)('Load answered %s renders "%s" as a %s toast', async (outcome, copy, type) => {
    const user = userEvent.setup();

    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockIdleStatusWithAms)),
      http.post('/api/v1/printers/:id/ams/load', () =>
        HttpResponse.json({ outcome, message: BACKEND_MESSAGE }),
      ),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    const slots = screen.getAllByTestId('filament-slot');
    await hoverSlot(slots[0]);
    await user.click(screen.getByText('Load'));

    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(showToastSpy).toHaveBeenCalledWith(copy, type);
    expect(screen.queryByText(BACKEND_MESSAGE)).not.toBeInTheDocument();
  });

  it.each(unloadOutcomes)('Unload answered %s renders "%s" as a %s toast', async (outcome, copy, type) => {
    const user = userEvent.setup();

    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockIdleStatusWithAms)),
      http.post('/api/v1/printers/:id/ams/unload', () =>
        HttpResponse.json({ outcome, message: BACKEND_MESSAGE }),
      ),
    );

    render(<PrintersPage />);

    await waitFor(() => {
      expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
    });

    await user.click(screen.getByRole('button', { name: 'Unload' }));

    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(showToastSpy).toHaveBeenCalledWith(copy, type);
    expect(screen.queryByText(BACKEND_MESSAGE)).not.toBeInTheDocument();
  });

  describe('while a recovery driver is live', () => {
    const confirmCopy = en.printers.confirm;
    const confirmMessage = confirmCopy.amsRecoveryMessage.replace('{{name}}', mockPrinter.name);

    /** A wedge posture: PAUSE with an open `recovering` jam row; `driver_live` is the only gate. */
    function statusWithIncident(driverLive: boolean) {
      const incident: OpenIncidentState = {
        id: 304,
        kind: 'jam',
        status: 'recovering',
        slot_desc: null,
        created_at: null,
        operator_exits: false,
        driver_live: driverLive,
      };
      return { ...mockIdleStatusWithAms, state: 'PAUSE', open_incident: incident };
    }

    function recoveryDialog() {
      return screen.queryByRole('dialog', { name: confirmCopy.amsRecoveryTitle });
    }

    async function renderCard(status: object) {
      server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)));
      render(<PrintersPage />);
      await waitFor(() => {
        expect(screen.getAllByTestId('filament-slot').length).toBeGreaterThan(0);
      });
    }

    /** Status fixtures on which a click must send at once, with no dialog. */
    const sendsAtOnce: Array<[string, object]> = [
      ['driver_live false', statusWithIncident(false)],
      ['no open incident', mockIdleStatusWithAms],
    ];

    it('Load asks first: Cancel sends nothing, Send posts the tray and closes', async () => {
      const user = userEvent.setup();
      const sentTrayIds: Array<string | null> = [];
      server.use(
        http.post('/api/v1/printers/:id/ams/load', ({ request }) => {
          sentTrayIds.push(new URL(request.url).searchParams.get('tray_id'));
          return HttpResponse.json({ outcome: 'held', message: BACKEND_MESSAGE });
        }),
      );
      await renderCard(statusWithIncident(true));

      const slots = screen.getAllByTestId('filament-slot');
      await hoverSlot(slots[2]);
      await user.click(screen.getByText('Load'));

      const dialog = await screen.findByRole('dialog', { name: confirmCopy.amsRecoveryTitle });
      expect(within(dialog).getByText(confirmMessage)).toBeInTheDocument();
      await user.click(within(dialog).getByRole('button', { name: en.common.cancel }));
      expect(recoveryDialog()).not.toBeInTheDocument();

      await hoverSlot(slots[2]);
      await user.click(screen.getByText('Load'));
      const again = await screen.findByRole('dialog', { name: confirmCopy.amsRecoveryTitle });
      await user.click(within(again).getByRole('button', { name: confirmCopy.amsCommandButton }));
      expect(recoveryDialog()).not.toBeInTheDocument();

      // The toast lands after the confirmed request's response, so any request the
      // first click or the Cancel had sent would already be recorded here.
      expect(await screen.findByText(toastCopy.amsLoadHeld)).toBeInTheDocument();
      expect(sentTrayIds).toEqual(['2']);
    });

    it('Unload asks first: Cancel sends nothing, Send posts the unload and closes', async () => {
      const user = userEvent.setup();
      let unloadCalls = 0;
      server.use(
        http.post('/api/v1/printers/:id/ams/unload', () => {
          unloadCalls += 1;
          return HttpResponse.json({ outcome: 'held', message: BACKEND_MESSAGE });
        }),
      );
      await renderCard(statusWithIncident(true));

      await user.click(screen.getByRole('button', { name: 'Unload' }));
      const dialog = await screen.findByRole('dialog', { name: confirmCopy.amsRecoveryTitle });
      expect(within(dialog).getByText(confirmMessage)).toBeInTheDocument();
      await user.click(within(dialog).getByRole('button', { name: en.common.cancel }));
      expect(recoveryDialog()).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Unload' }));
      const again = await screen.findByRole('dialog', { name: confirmCopy.amsRecoveryTitle });
      await user.click(within(again).getByRole('button', { name: confirmCopy.amsCommandButton }));
      expect(recoveryDialog()).not.toBeInTheDocument();

      expect(await screen.findByText(toastCopy.amsUnloadHeld)).toBeInTheDocument();
      expect(unloadCalls).toBe(1);
    });

    it.each(sendsAtOnce)('Load with %s posts at once with no dialog', async (_label, status) => {
      const user = userEvent.setup();
      let captured: string | null = null;
      server.use(
        http.post('/api/v1/printers/:id/ams/load', ({ request }) => {
          captured = new URL(request.url).searchParams.get('tray_id');
          return HttpResponse.json({ outcome: 'acted', message: BACKEND_MESSAGE });
        }),
      );
      await renderCard(status);

      const slots = screen.getAllByTestId('filament-slot');
      await hoverSlot(slots[1]);
      await user.click(screen.getByText('Load'));
      expect(recoveryDialog()).not.toBeInTheDocument();

      await waitFor(() => {
        expect(captured).toBe('1');
      });
    });

    it.each(sendsAtOnce)('Unload with %s posts at once with no dialog', async (_label, status) => {
      const user = userEvent.setup();
      let unloadCalled = false;
      server.use(
        http.post('/api/v1/printers/:id/ams/unload', () => {
          unloadCalled = true;
          return HttpResponse.json({ outcome: 'acted', message: BACKEND_MESSAGE });
        }),
      );
      await renderCard(status);

      await user.click(screen.getByRole('button', { name: 'Unload' }));
      expect(recoveryDialog()).not.toBeInTheDocument();

      await waitFor(() => {
        expect(unloadCalled).toBe(true);
      });
    });
  });
});
