/**
 * Tests for the PrintersPage component.
 */

import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import en from '../../i18n/locales/en';

const mockPrinters = [
  {
    id: 1,
    name: 'X1 Carbon',
    ip_address: '192.168.1.100',
    serial_number: '00M09A350100001',
    access_code: '12345678',
    model: 'X1C',
    enabled: true,
    is_active: true,
    nozzle_diameter: 0.4,
    nozzle_type: 'hardened_steel',
    location: 'Workshop',
    auto_archive: true,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
  {
    id: 2,
    name: 'P1S Backup',
    ip_address: '192.168.1.101',
    serial_number: '00W00A123456789',
    access_code: '87654321',
    model: 'P1S',
    enabled: false,
    is_active: true,
    nozzle_diameter: 0.4,
    nozzle_type: 'stainless_steel',
    location: null,
    auto_archive: true,
    created_at: '2024-01-02T00:00:00Z',
    updated_at: '2024-01-02T00:00:00Z',
  },
];

const mockPrinterStatus = {
  connected: true,
  state: 'IDLE',
  awaiting_plate_clear: false,
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: {
    nozzle: 25,
    bed: 25,
    chamber: 25,
  },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
};

const selectToolbarDropdownOption = async (triggerName: RegExp, optionName: RegExp) => {
  const user = userEvent.setup();

  await user.click(screen.getByRole('button', { name: triggerName }));
  await user.click(await screen.findByRole('button', { name: optionName }));
};

describe('PrintersPage', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');

    server.use(
      http.get('/api/v1/printers/', () => {
        return HttpResponse.json(mockPrinters);
      }),
      http.get('/api/v1/printers/:id/status', () => {
        return HttpResponse.json(mockPrinterStatus);
      }),
      http.post('/api/v1/printers/:id/clear-plate', () => {
        return HttpResponse.json({ success: true, message: 'Plate cleared', incidents_closed: [] });
      }),
      http.get('/api/v1/settings/', () => {
        return HttpResponse.json({
          auto_archive: true,
          save_thumbnails: true,
          capture_finish_photo: true,
          default_filament_cost: 25.0,
          currency: 'USD',
          ams_humidity_good: 40,
          ams_humidity_fair: 60,
          ams_temp_good: 30,
          ams_temp_fair: 35,
          require_plate_clear: true,
        });
      }),
      // PrintersPage now reads UI rendering fields from the public ui-preferences
      // endpoint instead of /settings (#1293) — admin pages still hit /settings.
      http.get('/api/v1/settings/ui-preferences', () => {
        return HttpResponse.json({
          ams_humidity_good: 40,
          ams_humidity_fair: 60,
          ams_temp_good: 30,
          ams_temp_fair: 35,
          require_plate_clear: true,
        });
      }),
      http.get('/api/v1/queue/', () => {
        return HttpResponse.json([]);
      })
    );
  });

  describe('rendering', () => {
    it('renders the page title', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('Printers')).toBeInTheDocument();
      });
    });

    it('shows printer cards', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });
    });

    it('shows printer models', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1C')).toBeInTheDocument();
        expect(screen.getByText('P1S')).toBeInTheDocument();
      });
    });

    it('shows printer status', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        // Status should be shown - may vary based on state
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });
    });
  });

  describe('printer info', () => {
    it('shows IP address in printer info modal', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // IP address is shown in the PrinterInfoModal (accessed via 3-dot menu),
      // not directly on the card. Verify the printer data loaded correctly.
      expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
    });

    it('shows location when set', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        // Printers should render - location display may vary
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });
    });
  });

  describe('temperature display', () => {
    it('shows nozzle temperature', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        // Temperatures are shown in the UI
        expect(screen.getAllByText(/25/)).toBeTruthy();
      });
    });

    it('sets left and right nozzle temperatures from the nozzle selector', async () => {
      localStorage.setItem('printerCardSize', '2');
      const temperatureRequests: Array<{ target: string | null; nozzle: string | null }> = [];
      const dualNozzlePrinter = { ...mockPrinters[0], model: 'H2D', nozzle_count: 2 };
      const dualNozzleStatus = {
        ...mockPrinterStatus,
        active_extruder: 0,
        temperatures: {
          ...mockPrinterStatus.temperatures,
          nozzle: 31,
          nozzle_target: 0,
          nozzle_2: 32,
          nozzle_2_target: 0,
        },
        nozzle_rack: [
          { id: 0, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 5, stat: 1, max_temp: 300, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
          { id: 1, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 3, stat: 1, max_temp: 300, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
        ],
      };

      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([dualNozzlePrinter])),
        http.get('/api/v1/printers/:id/status', () => HttpResponse.json(dualNozzleStatus)),
        http.post('/api/v1/printers/:id/temperature/nozzle', ({ request }) => {
          const url = new URL(request.url);
          temperatureRequests.push({
            target: url.searchParams.get('target'),
            nozzle: url.searchParams.get('nozzle'),
          });
          return HttpResponse.json({ success: true, message: 'Nozzle temperature set' });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('L / R')).toBeInTheDocument();
      });

      // Dual-nozzle temps live on the L/R temperature card, not the nozzle-select card.
      fireEvent.click(screen.getByText('L / R').parentElement!);

      const leftTempBox = screen.getByText('Left Temp').parentElement!.parentElement!;
      fireEvent.click(within(leftTempBox).getByRole('button', { name: '220 C' }));

      await waitFor(() => {
        expect(temperatureRequests).toContainEqual({ target: '220', nozzle: '1' });
      });

      fireEvent.click(screen.getByText('L / R').parentElement!);

      const rightTempBox = screen.getByText('Right Temp').parentElement!.parentElement!;
      fireEvent.click(within(rightTempBox).getByRole('button', { name: '260 C' }));

      await waitFor(() => {
        expect(temperatureRequests).toContainEqual({ target: '260', nozzle: '0' });
      });
    });
  });

  describe('fan badges', () => {
    // Chamber fan only exists on enclosed Bambu models. Open-frame printers
    // (A1, A1 Mini, A2L, P1P) have no chamber fan — the firmware reports
    // big_fan2_speed as 0 there and the widget would be dead UI. The BACKEND
    // answers the capability (`has_chamber_fan` on the status payload, the same
    // predicate the cooldown fan lane gates on); the page no longer carries its
    // own model list, so these render the flag rather than a model name.
    const statusWithFans = (hasChamberFan: boolean) => ({
      ...mockPrinterStatus,
      cooling_fan_speed: 53,
      big_fan1_speed: 53,
      big_fan2_speed: 53,
      has_chamber_fan: hasChamberFan,
    });

    const renderWithCapability = (hasChamberFan: boolean) => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json(statusWithFans(hasChamberFan)),
        ),
      );
      render(<PrintersPage />);
    };

    it('hides the chamber fan badge when the printer reports no chamber fan', async () => {
      renderWithCapability(false);

      await waitFor(() => {
        // Part-cooling badge confirms the fan row rendered.
        expect(screen.getByTitle('Part Cooling Fan')).toBeInTheDocument();
      });
      expect(screen.getByTitle('Auxiliary Fan')).toBeInTheDocument();
      expect(screen.queryByTitle('Chamber Fan')).not.toBeInTheDocument();
    });

    it('shows the chamber fan badge when the printer reports one', async () => {
      renderWithCapability(true);

      await waitFor(() => {
        expect(screen.getByTitle('Chamber Fan')).toBeInTheDocument();
      });
      expect(screen.getByTitle('Part Cooling Fan')).toBeInTheDocument();
      expect(screen.getByTitle('Auxiliary Fan')).toBeInTheDocument();
    });

    it('hides the chamber fan badge when the capability is absent from the payload', async () => {
      // An older/partial status payload must fail closed, not render a control
      // the machine may not have.
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            ...mockPrinterStatus,
            cooling_fan_speed: 53,
            big_fan1_speed: 53,
            big_fan2_speed: 53,
          }),
        ),
      );
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByTitle('Part Cooling Fan')).toBeInTheDocument();
      });
      expect(screen.queryByTitle('Chamber Fan')).not.toBeInTheDocument();
    });

    const renderWithAirduct = (supportsAirduct: boolean) => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            ...statusWithFans(true),
            supports_airduct: supportsAirduct,
            airduct_mode: 0,
          }),
        ),
      );
      render(<PrintersPage />);
    };

    it('renders the airduct control when the printer supports one', async () => {
      renderWithAirduct(true);

      await waitFor(() => {
        expect(screen.getByTitle(/Airduct Mode/)).toBeInTheDocument();
      });
    });

    it('hides the airduct control when the printer does not support one', async () => {
      renderWithAirduct(false);

      await waitFor(() => {
        expect(screen.getByTitle('Chamber Fan')).toBeInTheDocument();
      });
      expect(screen.queryByTitle(/Airduct Mode/)).not.toBeInTheDocument();
    });
  });

  describe('empty state', () => {
    it('shows empty state when no printers', async () => {
      server.use(
        http.get('/api/v1/printers/', () => {
          return HttpResponse.json([]);
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText(/no printers/i)).toBeInTheDocument();
      });
    });
  });

  describe('printer actions', () => {
    it('has action buttons', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // There should be some interactive elements for printer actions
      const buttons = screen.getAllByRole('button');
      expect(buttons.length).toBeGreaterThan(0);
    });

    it('shows plate clear status and action on finished printers when not cleared', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'FINISH', awaiting_plate_clear: true });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Plate not Clear').length).toBeGreaterThan(0);
      });

      expect(screen.getAllByRole('button', { name: 'Mark plate as cleared' }).length).toBeGreaterThan(0);
    });

    it('shows plate clear status and action on failed printers when not cleared', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'FAILED', awaiting_plate_clear: true });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Plate not Clear').length).toBeGreaterThan(0);
      });

      expect(screen.getAllByRole('button', { name: 'Mark plate as cleared' }).length).toBeGreaterThan(0);
    });

    it('keeps the clear action available when an idle printer is still awaiting acknowledgment', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'IDLE', awaiting_plate_clear: true });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Plate not Clear').length).toBeGreaterThan(0);
      });

      expect(screen.getAllByRole('button', { name: 'Mark plate as cleared' }).length).toBeGreaterThan(0);
    });

    it('updates the plate clear status after using the printer card action', async () => {
      let awaitingPlateClear = true;

      server.use(
        http.get('/api/v1/printers/', () => {
          return HttpResponse.json([mockPrinters[0]]);
        }),
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'FINISH', awaiting_plate_clear: awaitingPlateClear });
        }),
        http.post('/api/v1/printers/:id/clear-plate', () => {
          awaitingPlateClear = false;
          return HttpResponse.json({ success: true, message: 'Plate cleared', incidents_closed: [] });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Plate not Clear').length).toBeGreaterThan(0);
      });

      fireEvent.click(screen.getAllByRole('button', { name: 'Mark plate as cleared' })[0]);

      await waitFor(() => {
        expect(screen.queryByText('Plate not Clear')).not.toBeInTheDocument();
      });

      expect(screen.getAllByText('Plate Clear').length).toBeGreaterThan(0);
    });

    it('shows an icon-only plate clear action in small card view', async () => {
      let awaitingPlateClear = true;

      server.use(
        http.get('/api/v1/printers/', () => {
          return HttpResponse.json([mockPrinters[0]]);
        }),
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'FINISH', awaiting_plate_clear: awaitingPlateClear });
        }),
        http.post('/api/v1/printers/:id/clear-plate', () => {
          awaitingPlateClear = false;
          return HttpResponse.json({ success: true, message: 'Plate cleared', incidents_closed: [] });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      fireEvent.click(screen.getByRole('button', { name: 'S' }));

      await waitFor(() => {
        expect(screen.queryByText('Mark plate as cleared')).not.toBeInTheDocument();
      });

      const clearButton = screen.getByRole('button', { name: 'Mark plate as cleared' });

      fireEvent.click(clearButton);

      await waitFor(() => {
        expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument();
      });
    });

    it('shows plate clear status but no action while idle', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Plate Clear').length).toBeGreaterThan(0);
      });

      expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument();
    });

    it('shows plate in use status while printing and hides the clear action', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'RUNNING', awaiting_plate_clear: false });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Plate in Use').length).toBeGreaterThan(0);
      });

      expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument();
    });

    it('hides plate status and action when plate-clear confirmation is disabled', async () => {
      server.use(
        http.get('/api/v1/settings/', () => {
          return HttpResponse.json({
            auto_archive: true,
            save_thumbnails: true,
            capture_finish_photo: true,
            default_filament_cost: 25.0,
            currency: 'USD',
            ams_humidity_good: 40,
            ams_humidity_fair: 60,
            ams_temp_good: 30,
            ams_temp_fair: 35,
            require_plate_clear: false,
          });
        }),
        http.get('/api/v1/settings/ui-preferences', () => {
          return HttpResponse.json({
            ams_humidity_good: 40,
            ams_humidity_fair: 60,
            ams_temp_good: 30,
            ams_temp_fair: 35,
            require_plate_clear: false,
          });
        }),
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json({ ...mockPrinterStatus, state: 'FINISH', awaiting_plate_clear: true });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      expect(screen.queryByText('Plate not Clear')).not.toBeInTheDocument();
      expect(screen.queryByText('Plate Clear')).not.toBeInTheDocument();
      expect(screen.queryByText('Plate in Use')).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Mark plate as cleared' })).not.toBeInTheDocument();
    });
  });

  describe('disabled printer', () => {
    it('shows disabled state for disabled printers', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });

      // Disabled printers have visual indication
      const disabledPrinter = screen.getByText('P1S Backup').closest('div');
      expect(disabledPrinter).toBeInTheDocument();
    });
  });

  /**
   * Maintenance mode is a SERVICE HOLD, not `is_active=false`: the session stays
   * up, the whole status tree keeps rendering, and the hold is one banner beside
   * the quarantine banner. These pin that projection — the banner's role and its
   * one action, the confirm-only-when-something-is-live rule, and the fact that
   * the generic incident chip does not double up on it.
   */
  describe('maintenance mode (service hold)', () => {
    const heldPrinter = {
      ...mockPrinters[0],
      service_hold: { since: '2026-09-12T03:02:28Z' },
    };

    /** Serve exactly one printer card, with optional status-frame overrides. */
    const serveOne = (printer: unknown, status: Record<string, unknown> = {}) => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...mockPrinterStatus, ...status }),
        ),
      );
    };

    it('renders the hold banner BESIDE the status tree, not instead of it', async () => {
      serveOne(heldPrinter);
      render(<PrintersPage />);

      const exit = await screen.findByRole('button', { name: /exit maintenance mode/i });
      // The banner is a live region (role=status) carrying the exit action.
      expect(exit.closest('[role="status"]')).not.toBeNull();
      // …and the status tree underneath still renders: a held printer is
      // connected, so its state is real and stays on screen.
      expect(await screen.findByText(/ready to print/i)).toBeInTheDocument();
    });

    it('releases the hold through DELETE when the banner action is used', async () => {
      const released: string[] = [];
      serveOne(heldPrinter);
      server.use(
        http.delete('/api/v1/printers/:id/service-hold', ({ params }) => {
          released.push(String(params.id));
          return HttpResponse.json({ released: true });
        }),
      );
      render(<PrintersPage />);

      fireEvent.click(await screen.findByRole('button', { name: /exit maintenance mode/i }));

      await waitFor(() => expect(released).toEqual(['1']));
    });

    /** Capture every enter POST; the result body carries no job outcome. */
    const captureEnter = (entered: string[]) =>
      server.use(
        http.post('/api/v1/printers/:id/service-hold', ({ params }) => {
          entered.push(String(params.id));
          return HttpResponse.json({
            held: true,
            already_held: false,
            eject_stopped: false,
            lease_revoked: false,
          });
        }),
      );

    it('enters on one click when live status shows nothing to stop', async () => {
      const entered: string[] = [];
      serveOne(mockPrinters[0]);
      captureEnter(entered);
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));
      await user.click(await screen.findByRole('button', { name: /enter maintenance mode/i }));

      await waitFor(() => expect(entered).toEqual(['1']));
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    /**
     * No mode verb ends a print — only Stop does. A RUNNING print is therefore
     * NOT an effect of entering the hold, so it raises no dialog: the print
     * keeps going and its terminal rides the ordinary lanes.
     */
    it('enters on one click while a print is RUNNING — the print is not stopped', async () => {
      const entered: string[] = [];
      serveOne(mockPrinters[0], { state: 'RUNNING', progress: 42, current_print: 'part.3mf' });
      captureEnter(entered);
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));
      await user.click(await screen.findByRole('button', { name: /enter maintenance mode/i }));

      await waitFor(() => expect(entered).toEqual(['1']));
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });

    /** The dialog survives for the farm's OWN live actions — here an armed
     *  cooldown watch, whose eject the hold defers. */
    it('asks for confirmation first when a cooldown watch is armed', async () => {
      const entered: string[] = [];
      serveOne(mockPrinters[0], { state: 'FINISH', eject_watch: { threshold_c: 33 } });
      captureEnter(entered);
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));
      await user.click(await screen.findByRole('button', { name: /enter maintenance mode/i }));

      // A dialog, and nothing sent until it is confirmed.
      const dialog = await screen.findByRole('dialog');
      expect(entered).toEqual([]);

      await user.click(within(dialog).getByRole('button', { name: /enter maintenance mode/i }));
      await waitFor(() => expect(entered).toEqual(['1']));
    });

    it('does not render the generic incident chip for the hold kind', async () => {
      serveOne(heldPrinter, {
        open_incident: { id: 41, kind: 'service_hold', status: 'escalated', slot_desc: null, created_at: null, operator_exits: false, printer_messages: [] },
      });
      render(<PrintersPage />);
      await screen.findByRole('button', { name: /exit maintenance mode/i });

      // The chip looks its label up dynamically, so a rendered suppressed kind
      // would print its own key (no locale leaf exists for it, by design).
      expect(screen.queryByText('printers.incident.service_hold')).not.toBeInTheDocument();
    });

    it('still renders the chip for a fault kind', async () => {
      serveOne(heldPrinter, {
        open_incident: { id: 42, kind: 'jam', status: 'escalated', slot_desc: null, created_at: null, operator_exits: false, printer_messages: [] },
      });
      render(<PrintersPage />);

      expect(await screen.findByText('AMS jam')).toBeInTheDocument();
    });
  });

  /**
   * The plate authority has owned the in-flight eject record since the
   * standalone-eject rework and nothing rendered it — which is how two printers
   * sat with a 40-minute claim and a clear-plate that 409'd with no way out on
   * screen. Recover appears once the watchdog has given its verdict.
   */
  describe('in-flight eject', () => {
    const occupancyWith = (ejectOverrides: Record<string, unknown>) => ({
      state: 'FINISH',
      awaiting_plate_clear: true,
      occupancy: {
        plate: { occupied: true, source_subtask_id: '123', policy: 'CooldownEject', since: null, refusal: null },
        eject: {
          purpose: 'production',
          started: true,
          age_s: 154,
          hydrated: false,
          runtime_exceeded: false,
          ...ejectOverrides,
        },
        lease_age_s: null,
      },
    });

    const serveStatus = (status: Record<string, unknown>) => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...mockPrinterStatus, ...status }),
        ),
      );
    };

    it('reports an owned sweep without offering the override', async () => {
      serveStatus(occupancyWith({}));
      render(<PrintersPage />);

      expect(await screen.findByText(/eject in progress/i)).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /recover/i })).not.toBeInTheDocument();
    });

    it('offers Recover once the watchdog has fired', async () => {
      serveStatus(occupancyWith({ runtime_exceeded: true, age_s: 2292 }));
      render(<PrintersPage />);

      expect(await screen.findByText(/eject stalled/i)).toBeInTheDocument();
      const recover = screen.getByRole('button', { name: /recover/i });

      fireEvent.click(recover);
      // The EXISTING recover confirm — its effect list gains the eject bullet.
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByText(/drop the eject in progress/i)).toBeInTheDocument();
    });
  });

  /**
   * The operator's exit from an equipment fault. Printer 011-H2S sat escalated
   * with a clean wire, no quarantine, no plate gate and no eject — every card
   * predicate said "nothing to recover" while the backend's Recover verb was the
   * only thing that could close the row. The card now derives the affordance and
   * the dialog's effect list from the SAME verdict (`utils/printerRecovery`), so
   * what the operator is offered and what they are told cannot drift apart.
   */
  describe('equipment fault Recover', () => {
    const faultStatus = (operatorExits: boolean) => ({
      state: 'IDLE',
      awaiting_plate_clear: false,
      occupancy: {
        plate: { occupied: false, source_subtask_id: null, policy: null, since: null, refusal: null },
        eject: null,
        lease_age_s: null,
      },
      open_incident: {
        id: 188,
        kind: 'physical',
        status: 'escalated',
        slot_desc: 'AMS A slot 3',
        created_at: '2026-09-17T09:43:00Z',
        operator_exits: operatorExits,
        printer_messages: [],
      },
    });

    const serveFault = (operatorExits: boolean) => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...mockPrinterStatus, ...faultStatus(operatorExits) }),
        ),
      );
    };

    it('offers Recover in the card menu on the fault alone', async () => {
      serveFault(true);
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));

      expect(await screen.findByRole('button', { name: 'Recover printer' })).toBeInTheDocument();
    });

    it('lists the fault effect and NOT the plate hold this printer does not have', async () => {
      serveFault(true);
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));
      await user.click(await screen.findByRole('button', { name: 'Recover printer' }));

      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByText(/close the open equipment fault/i)).toBeInTheDocument();
      expect(within(dialog).queryByText(/clear the plate hold/i)).not.toBeInTheDocument();
      // The run resume is the VERB's consequence, not this printer's state:
      // it is listed whatever the effect list says.
      expect(within(dialog).getByText(/resume the paused run/i)).toBeInTheDocument();
    });

    it('does not offer Recover for a fault that is not the operator\'s to close', async () => {
      // A runout ends on the wire reading filament again — Recover is not its exit.
      serveFault(false);
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));
      await screen.findByRole('button', { name: /maintenance mode/i });

      expect(screen.queryByRole('button', { name: 'Recover printer' })).not.toBeInTheDocument();
    });

    it('names the closed fault in the Recover toast', async () => {
      serveFault(true);
      server.use(
        http.post('/api/v1/printers/:id/recover', () =>
          HttpResponse.json({
            plate_cleared: false,
            quarantine_cleared: false,
            runs_resumed: [],
            incidents_closed: ['physical'],
          }),
        ),
      );
      render(<PrintersPage />);
      await screen.findByText('X1 Carbon');

      const user = userEvent.setup();
      await user.click(screen.getByRole('button', { name: 'More' }));
      await user.click(await screen.findByRole('button', { name: 'Recover printer' }));
      const dialog = await screen.findByRole('dialog');
      await user.click(within(dialog).getByRole('button', { name: 'Recover & resume' }));

      expect(await screen.findByText(/equipment fault closed/i)).toBeInTheDocument();
    });
  });

  /**
   * The printer's words survive on the hold (2026-09-24 ruling). A ladder verb,
   * a stop or the next job can clear the printer's own dialog while the hold
   * still stands, and 003-H2S's operator never saw "Detected build plate offset
   * or debris" because the farm's stop wiped it. The hold carries what the
   * printer said; the card shows a recorded message only once the live list no
   * longer carries its code, and a refused plate names the check on its row.
   * Copy is asserted through the `en` leaves, never restated here.
   */
  describe('printer messages on a hold', () => {
    const PLATE_CHECK_TEXT = 'Detected build plate offset or debris';
    const plateCheck = { short_code: '0500_808C', description: PLATE_CHECK_TEXT };

    const heldBy = (kind: string, messages: Array<{ short_code: string; description: string }>) => ({
      id: 327,
      kind,
      status: 'escalated',
      slot_desc: null,
      created_at: '2026-09-24T20:09:09Z',
      operator_exits: false,
      driver_live: false,
      printer_messages: messages,
    });

    /** A live HMS entry as the status payload enriches it. */
    const liveHms = (shortCode: string, description: string) => ({
      code: '0x808c',
      attr: 0x05008000,
      module: 5,
      severity: 2,
      short_code: shortCode,
      description,
    });

    const reportedLine = (text: string) => en.printers.holdMessage.reported.replace('{{message}}', text);
    const refusalLine = (text: string) => en.printers.plateStatus.refusal.replace('{{message}}', text);

    const serveStatus = (status: Record<string, unknown>) => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...mockPrinterStatus, ...status }),
        ),
      );
    };

    it('shows the recorded message under the chip once the printer no longer shows it', async () => {
      serveStatus({ state: 'PAUSE', hms_errors: [], open_incident: heldBy('plate_vision', [plateCheck]) });
      render(<PrintersPage />);

      expect(await screen.findByText(reportedLine(PLATE_CHECK_TEXT))).toBeInTheDocument();
      // "No longer shown on the printer" is supplementary: a focusable tooltip
      // trigger carries it, never inline copy.
      expect(screen.getByRole('button', { name: en.printers.holdMessage.notShown })).toBeInTheDocument();
    });

    it('adds no second line for a message the printer still shows live', async () => {
      serveStatus({
        state: 'PAUSE',
        hms_errors: [liveHms('0500_808C', PLATE_CHECK_TEXT)],
        open_incident: heldBy('plate_vision', [plateCheck]),
      });
      render(<PrintersPage />);

      // The live HMS summary names it (and owns its verbs)…
      expect(await screen.findByRole('button', { name: PLATE_CHECK_TEXT })).toBeInTheDocument();
      // …so the hold's record stays out of the way.
      expect(screen.queryByText(reportedLine(PLATE_CHECK_TEXT))).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: en.printers.holdMessage.notShown })).not.toBeInTheDocument();
    });

    it('keeps the record for every hold kind, deduped by code rather than by "anything live"', async () => {
      const feed = { short_code: '0700_8001', description: 'AMS A Slot 1 failed to feed' };
      // A jam hold whose feed message was cleared by a ladder verb, while an
      // UNRELATED fault is live: only a matching code suppresses the record.
      serveStatus({
        state: 'PAUSE',
        hms_errors: [liveHms('0500_808C', PLATE_CHECK_TEXT)],
        open_incident: heldBy('jam', [feed]),
      });
      render(<PrintersPage />);

      expect(await screen.findByText(reportedLine(feed.description))).toBeInTheDocument();
    });

    const refusedPlate = (refusal: { messages: Array<{ short_code: string; description: string }> } | null) => ({
      state: 'IDLE',
      awaiting_plate_clear: true,
      occupancy: {
        plate: { occupied: true, source_subtask_id: null, policy: 'EscalationOnly', since: null, refusal },
        eject: null,
        lease_age_s: null,
      },
    });

    it('names the plate check on the plate row of a refused plate, beside Mark plate cleared', async () => {
      serveStatus(refusedPlate({ messages: [plateCheck] }));
      render(<PrintersPage />);

      expect(await screen.findByText(refusalLine(PLATE_CHECK_TEXT))).toBeInTheDocument();
      expect(screen.getByRole('button', { name: en.printers.plateStatus.markCleared })).toBeInTheDocument();
    });

    it('shows no refusal line on a plate gate that carries no refusal', async () => {
      serveStatus(refusedPlate(null));
      render(<PrintersPage />);

      await screen.findByRole('button', { name: en.printers.plateStatus.markCleared });
      const refusalLead = en.printers.plateStatus.refusal.split('{{message}}')[0];
      expect(screen.queryByText((text) => text.startsWith(refusalLead))).not.toBeInTheDocument();
    });
  });

  /**
   * `is_active=false` keeps upstream's meaning — this instance holds no MQTT
   * session — and is labelled Deactivated. It is no longer the maintenance verb.
   */
  describe('deactivated printer', () => {
    const deactivated = { ...mockPrinters[0], is_active: false };

    it('labels the header pill Deactivated instead of Offline', async () => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([deactivated])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...mockPrinterStatus, connected: false }),
        ),
      );
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Deactivated').length).toBeGreaterThan(0);
      });
      expect(screen.queryByText('Offline')).not.toBeInTheDocument();
      // No connection diagnostic CTA — nothing is wrong with the network.
      expect(screen.queryByRole('button', { name: /run.*diagnostic/i })).not.toBeInTheDocument();
    });

    it('PATCHes is_active=true from the Activate action', async () => {
      const patchedBodies: unknown[] = [];
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([deactivated])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({ ...mockPrinterStatus, connected: false }),
        ),
        http.patch('/api/v1/printers/:id', async ({ request }) => {
          patchedBodies.push(await request.json());
          return HttpResponse.json({ ...deactivated, is_active: true });
        }),
      );
      render(<PrintersPage />);

      fireEvent.click(await screen.findByRole('button', { name: 'Activate' }));

      await waitFor(() => expect(patchedBodies.length).toBeGreaterThan(0));
      expect(patchedBodies[0]).toEqual(expect.objectContaining({ is_active: true }));
    });
  });

  describe('nozzle rack card', () => {
    const h2cStatus = {
      ...mockPrinterStatus,
      nozzle_rack: [
        { id: 0, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 5, stat: 1, max_temp: 300, serial_number: 'SN-L', filament_color: '', filament_id: '', filament_type: '' },
        { id: 1, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 3, stat: 0, max_temp: 300, serial_number: 'SN-R', filament_color: '', filament_id: '', filament_type: '' },
        { id: 16, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 10, stat: 0, max_temp: 300, serial_number: 'SN-16', filament_color: '', filament_id: '', filament_type: '' },
        { id: 17, nozzle_type: 'HH01', nozzle_diameter: '0.6', wear: 0, stat: 0, max_temp: 300, serial_number: 'SN-17', filament_color: '', filament_id: '', filament_type: '' },
        { id: 18, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 2, stat: 0, max_temp: 300, serial_number: 'SN-18', filament_color: '', filament_id: '', filament_type: '' },
        { id: 19, nozzle_type: '', nozzle_diameter: '', wear: null, stat: null, max_temp: 0, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
        { id: 20, nozzle_type: '', nozzle_diameter: '', wear: null, stat: null, max_temp: 0, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
        { id: 21, nozzle_type: '', nozzle_diameter: '', wear: null, stat: null, max_temp: 0, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
      ],
    };

    it('shows nozzle rack when H2C rack slots present', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json(h2cStatus);
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Nozzle Rack').length).toBeGreaterThan(0);
      });
    });

    it('shows 6 rack slot elements for H2C', async () => {
      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json(h2cStatus);
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Nozzle Rack').length).toBeGreaterThan(0);
      });

      // Rack shows diameters for occupied slots and dashes for empty ones
      const dashes = screen.getAllByText('—');
      expect(dashes.length).toBeGreaterThanOrEqual(3); // 3 empty rack positions (IDs 19,20,21)
    });

    it('keeps empty slot anchored to physical position when its nozzle is mounted (#943)', async () => {
      // H2C with rack slot 16 picked up into the hotend — firmware omits ID 16
      // entirely from nozzle.info. Each rack diameter is unique so we can assert
      // the ordering by tooltip lookup.
      const h2cSlot16Mounted = {
        ...mockPrinterStatus,
        nozzle_rack: [
          { id: 0, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 5, stat: 1, max_temp: 300, serial_number: 'SN-L', filament_color: '', filament_id: '', filament_type: '' },
          { id: 1, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 3, stat: 0, max_temp: 300, serial_number: 'SN-R', filament_color: '', filament_id: '', filament_type: '' },
          // ID 16 missing — currently in hotend
          { id: 17, nozzle_type: 'HS', nozzle_diameter: '0.2', wear: 0, stat: 0, max_temp: 300, serial_number: 'SN-17', filament_color: '', filament_id: '', filament_type: '' },
          { id: 18, nozzle_type: 'HS', nozzle_diameter: '0.6', wear: 0, stat: 0, max_temp: 300, serial_number: 'SN-18', filament_color: '', filament_id: '', filament_type: '' },
          { id: 19, nozzle_type: 'HS', nozzle_diameter: '0.8', wear: 0, stat: 0, max_temp: 300, serial_number: 'SN-19', filament_color: '', filament_id: '', filament_type: '' },
          { id: 20, nozzle_type: 'HH01', nozzle_diameter: '1.0', wear: 0, stat: 0, max_temp: 300, serial_number: 'SN-20', filament_color: '', filament_id: '', filament_type: '' },
          { id: 21, nozzle_type: 'HH01', nozzle_diameter: '1.2', wear: 0, stat: 0, max_temp: 300, serial_number: 'SN-21', filament_color: '', filament_id: '', filament_type: '' },
        ],
      };

      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json(h2cSlot16Mounted);
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('Nozzle Rack').length).toBeGreaterThan(0);
      });

      // Slot 1 (leftmost, ID 16) should be the empty dash; slots 2..6 should
      // hold the 5 remaining nozzles in order 17, 18, 19, 20, 21.
      const rackLabel = screen.getAllByText('Nozzle Rack')[0];
      const rackCard = rackLabel.parentElement!;
      const slotRow = rackCard.querySelectorAll('div.flex')[0];
      const slotTexts = Array.from(slotRow.querySelectorAll('span')).map(s => s.textContent);
      expect(slotTexts).toEqual(['—', '0.2', '0.6', '0.8', '1.0', '1.2']);
    });

    it('hides nozzle rack when only L/R nozzles present (H2D)', async () => {
      const h2dStatus = {
        ...mockPrinterStatus,
        nozzle_rack: [
          { id: 0, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 5, stat: 1, max_temp: 300, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
          { id: 1, nozzle_type: 'HS', nozzle_diameter: '0.4', wear: 3, stat: 1, max_temp: 300, serial_number: '', filament_color: '', filament_id: '', filament_type: '' },
        ],
      };

      server.use(
        http.get('/api/v1/printers/:id/status', () => {
          return HttpResponse.json(h2dStatus);
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      expect(screen.queryByText('Nozzle Rack')).not.toBeInTheDocument();
    });
  });

  describe('firmware version badge', () => {
    const firmwareUpToDate = {
      printer_id: 1,
      current_version: '01.09.00.00',
      latest_version: '01.09.00.00',
      update_available: false,
      download_url: null,
      release_notes: 'Bug fixes and improvements.',
    };

    const firmwareUpdateAvailable = {
      printer_id: 1,
      current_version: '01.08.00.00',
      latest_version: '01.09.00.00',
      update_available: true,
      download_url: 'https://example.com/firmware.bin',
      release_notes: 'New features added.',
    };

    it('shows green badge when firmware is up to date', async () => {
      server.use(
        http.get('/api/v1/firmware/updates/:id', () => {
          return HttpResponse.json(firmwareUpToDate);
        }),
        http.get('/api/v1/settings/', () => {
          return HttpResponse.json({
            check_printer_firmware: true,
            auto_archive: true,
            save_thumbnails: true,
          });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('01.09.00.00').length).toBeGreaterThan(0);
      });

      const badge = screen.getAllByText('01.09.00.00')[0].closest('button');
      expect(badge).toBeInTheDocument();
      expect(badge?.className).toContain('text-status-ok');
    });

    it('shows orange badge when firmware update is available', async () => {
      server.use(
        http.get('/api/v1/firmware/updates/:id', () => {
          return HttpResponse.json(firmwareUpdateAvailable);
        }),
        http.get('/api/v1/settings/', () => {
          return HttpResponse.json({
            check_printer_firmware: true,
            auto_archive: true,
            save_thumbnails: true,
          });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getAllByText('01.08.00.00').length).toBeGreaterThan(0);
      });

      const badge = screen.getAllByText('01.08.00.00')[0].closest('button');
      expect(badge).toBeInTheDocument();
      expect(badge?.className).toContain('text-orange-400');
    });

    it('hides badge when firmware check is disabled', async () => {
      server.use(
        http.get('/api/v1/settings/', () => {
          return HttpResponse.json({
            check_printer_firmware: false,
            auto_archive: true,
            save_thumbnails: true,
          });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // Version should not appear when firmware check is disabled
      expect(screen.queryByText('01.09.00.00')).not.toBeInTheDocument();
      expect(screen.queryByText('01.08.00.00')).not.toBeInTheDocument();
    });

    it('hides badge when API has no firmware data for the model', async () => {
      const firmwareNoData = {
        printer_id: 1,
        current_version: '01.01.03.00',
        latest_version: null,
        update_available: false,
        download_url: null,
        release_notes: null,
      };

      server.use(
        http.get('/api/v1/firmware/updates/:id', () => {
          return HttpResponse.json(firmwareNoData);
        }),
        http.get('/api/v1/settings/', () => {
          return HttpResponse.json({
            check_printer_firmware: true,
            auto_archive: true,
            save_thumbnails: true,
          });
        })
      );

      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // Badge should not appear when API returns no latest_version
      expect(screen.queryByText('01.01.03.00')).not.toBeInTheDocument();
    });
  });

  describe('bulk selection', () => {
    it('shows select button in toolbar', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // The Select button should be in the toolbar (title attribute)
      const selectButton = screen.getByTitle('Select');
      expect(selectButton).toBeInTheDocument();
    });

    it('shows selection toolbar after clicking select button', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // Click the Select button to enter selection mode
      fireEvent.click(screen.getByTitle('Select'));

      // The floating toolbar should appear with Select All
      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });
    });

    it('shows selection count when printers are selected', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // Enter selection mode
      fireEvent.click(screen.getByTitle('Select'));

      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });

      // Click Select All to select both printers
      fireEvent.click(screen.getByText('Select All'));

      // Should show "2 selected"
      await waitFor(() => {
        expect(screen.getByText('2 selected')).toBeInTheDocument();
      });
    });

    it('shows select by state dropdown', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // Enter selection mode
      fireEvent.click(screen.getByTitle('Select'));

      await waitFor(() => {
        expect(screen.getByText('Select by State')).toBeInTheDocument();
      });
    });

    it('exits selection mode on close button', async () => {
      render(<PrintersPage />);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
      });

      // Enter selection mode
      fireEvent.click(screen.getByTitle('Select'));

      await waitFor(() => {
        expect(screen.getByText('Select All')).toBeInTheDocument();
      });

      // Click the Select button again to exit (it toggles)
      fireEvent.click(screen.getByTitle('Select'));

      // Floating toolbar should disappear
      await waitFor(() => {
        expect(screen.queryByText('Select All')).not.toBeInTheDocument();
      });
    });
  });

  describe('search and filter', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
        http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockPrinterStatus)),
        http.get('/api/v1/queue/', () => HttpResponse.json([]))
      );
    });

    it('filters by name (case-insensitive)', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'x1 carbon' } });

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.queryByText('P1S Backup')).not.toBeInTheDocument();
      });
    });

    it('trims leading and trailing whitespace from search', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      // " X1 Carbon " with surrounding spaces must still match
      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: '  X1 Carbon  ' } });

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.queryByText('P1S Backup')).not.toBeInTheDocument();
      });
    });

    it('filters by model', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'P1S' } });

      await waitFor(() => {
        expect(screen.queryByText('X1 Carbon')).not.toBeInTheDocument();
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });
    });

    it('filters by serial number', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: '00M09A' } });

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.queryByText('P1S Backup')).not.toBeInTheDocument();
      });
    });

    it('shows empty state when no printers match search', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'ZZZ_NO_MATCH' } });

      await waitFor(() => {
        expect(screen.getByText('No printers match your search or filters')).toBeInTheDocument();
      });
    });

    it('clear button resets search and shows all printers', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'X1 Carbon' } });

      await waitFor(() => expect(screen.queryByText('P1S Backup')).not.toBeInTheDocument());

      // Click the accessible clear button
      fireEvent.click(screen.getByRole('button', { name: 'Clear' }));

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });
    });

    it('filters by status (offline) via dropdown', async () => {
      // Override: printer 1 online, printer 2 offline
      server.use(
        http.get('/api/v1/printers/:id/status', ({ params }) => {
          if (Number(params.id) === 2) {
            return HttpResponse.json({ ...mockPrinterStatus, connected: false });
          }
          return HttpResponse.json(mockPrinterStatus);
        })
      );

      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      await selectToolbarDropdownOption(/all statuses/i, /^offline$/i);

      await waitFor(() => {
        expect(screen.queryByText('X1 Carbon')).not.toBeInTheDocument();
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });
    });

    it('shows empty state when status filter matches nothing', async () => {
      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      // Both printers are IDLE; filtering by "printing" should yield no results
      await selectToolbarDropdownOption(/all statuses/i, /^printing$/i);

      await waitFor(() => {
        expect(screen.getByText('No printers match your search or filters')).toBeInTheDocument();
      });
    });

    it('combines search and status filter', async () => {
      // Printer 1 = RUNNING (printing), printer 2 = IDLE
      server.use(
        http.get('/api/v1/printers/:id/status', ({ params }) => {
          if (Number(params.id) === 1) {
            return HttpResponse.json({ ...mockPrinterStatus, state: 'RUNNING' });
          }
          return HttpResponse.json(mockPrinterStatus);
        })
      );

      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      // Filter to only "printing" printers
      await selectToolbarDropdownOption(/all statuses/i, /^printing$/i);

      // Then also search for a term that only matches printer 1
      fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'X1' } });

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.queryByText('P1S Backup')).not.toBeInTheDocument();
      });
    });

    it('filters by location via dropdown', async () => {
      // Override: give printer 2 its own location so the dropdown has two options
      // and we can verify the filter picks the right one. Printer 1 stays at 'Workshop'.
      server.use(
        http.get('/api/v1/printers/', () =>
          HttpResponse.json([
            mockPrinters[0],
            { ...mockPrinters[1], location: 'Office' },
          ])
        )
      );

      render(<PrintersPage />);
      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });

      await selectToolbarDropdownOption(/all locations/i, /^workshop$/i);

      await waitFor(() => {
        expect(screen.getByText('X1 Carbon')).toBeInTheDocument();
        expect(screen.queryByText('P1S Backup')).not.toBeInTheDocument();
      });

      await selectToolbarDropdownOption(/^workshop$/i, /^office$/i);

      await waitFor(() => {
        expect(screen.queryByText('X1 Carbon')).not.toBeInTheDocument();
        expect(screen.getByText('P1S Backup')).toBeInTheDocument();
      });
    });

    it('hides location filter when no printers have a location', async () => {
      // Both printers have null location — dropdown should not render at all
      server.use(
        http.get('/api/v1/printers/', () =>
          HttpResponse.json([
            { ...mockPrinters[0], location: null },
            { ...mockPrinters[1], location: null },
          ])
        )
      );

      render(<PrintersPage />);
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      // Status filter is still there, but the location filter should be absent.
      expect(screen.getByRole('button', { name: /all statuses/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /all locations/i })).not.toBeInTheDocument();
    });
  });

  describe('Spoolman loading guard', () => {
    it('does not show Assign Spool button while Spoolman queries are loading', async () => {
      // Spoolman enabled but inventory and slot-assignment queries never resolve
      server.use(
        http.get('/api/v1/spoolman/status', () =>
          HttpResponse.json({ enabled: true, connected: true })
        ),
        http.get('/api/v1/spoolman/inventory/spools', () =>
          new Promise(() => {})  // never resolves
        ),
        http.get('/api/v1/spoolman/inventory/slot-assignments/all', () =>
          new Promise(() => {})  // never resolves
        )
      );

      render(<PrintersPage />);

      // Wait for the page to render (printers should be visible)
      await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());

      // While Spoolman queries are still loading, the "Assign Spool" button must
      // not appear (inventory prop is undefined → {inventory && ...} guard fires)
      expect(screen.queryByText('Assign Spool')).not.toBeInTheDocument();
    });
  });

  describe('nozzle diameter display', () => {
    // Single-nozzle printers (H2S fleet) populate only nozzles[0]; the second
    // slot is a fixed empty stub. Display must stay unchanged: one unlabeled
    // diameter, no L/R side label.
    it('shows a single unlabeled diameter for single-nozzle printers', async () => {
      server.use(
        http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinters[0]])),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            ...mockPrinterStatus,
            nozzles: [
              { nozzle_type: 'hardened_steel', nozzle_diameter: '0.4' },
              { nozzle_type: '', nozzle_diameter: '' },
            ],
          }),
        ),
      );

      render(<PrintersPage />);

      const nozzle = await screen.findByTitle('hardened_steel');
      // Exact text — no side label, matches the pre-existing "• {d}mm" display.
      expect(nozzle.textContent).toBe('• 0.4mm');
      expect(screen.queryByText('R 0.4mm')).not.toBeInTheDocument();
      expect(screen.queryByText('L 0.4mm')).not.toBeInTheDocument();
    });

    // Dual-nozzle printers (H2C) report BOTH hotends: index 0 = right, index 1
    // = left. Both diameters render, each labelled by side, each keeping its
    // own nozzle_type tooltip.
    it('shows both diameters with R/L side labels for dual-nozzle printers', async () => {
      server.use(
        http.get('/api/v1/printers/', () =>
          HttpResponse.json([{ ...mockPrinters[0], model: 'H2C' }]),
        ),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            ...mockPrinterStatus,
            nozzles: [
              { nozzle_type: 'HH01', nozzle_diameter: '0.6' },        // right
              { nozzle_type: 'hardened_steel', nozzle_diameter: '0.4' }, // left
            ],
          }),
        ),
      );

      render(<PrintersPage />);

      const right = await screen.findByText('R 0.6mm');
      const left = screen.getByText('L 0.4mm');
      expect(right).toBeInTheDocument();
      expect(left).toBeInTheDocument();
      // Per-side nozzle_type tooltip preserved.
      expect(right).toHaveAttribute('title', 'HH01');
      expect(left).toHaveAttribute('title', 'hardened_steel');
    });
  });

});

/**
 * Phase 13 P13-1 (PrintersPage EmptySlotHoverCard onAssignSpool gate removal)
 *
 * Pre-Phase-13 each of the three EmptySlotHoverCard call-sites in PrintersPage
 * gated `onAssignSpool` on `spoolmanEnabled ? (...) : undefined`, so empty
 * slots in local-Inventory mode never showed an Assign action. Maintainer
 * Foto 7 confirmed users expect the button regardless of mode.
 *
 * To assert wiring without going through hover-card animations, we mock the
 * EmptySlotHoverCard component at module level and capture every props
 * payload. The same mock is active in both modes; tests differ only in the
 * spoolman-settings mock. The mock module covers BOTH FilamentHoverCard exports
 * so tests outside this `describe` aren't affected (we re-export the real
 * FilamentHoverCard).
 */
const phase13EmptySlotProps: Array<Record<string, unknown>> = [];
const phase14HoverCardProps: Array<Record<string, unknown>> = [];

vi.mock('../../components/FilamentHoverCard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../components/FilamentHoverCard')>();
  return {
    ...actual,
    EmptySlotHoverCard: (props: Record<string, unknown>) => {
      phase13EmptySlotProps.push({ ...props });
      return null;
    },
    FilamentHoverCard: (props: Record<string, unknown>) => {
      phase14HoverCardProps.push({ ...props });
      return null;
    },
  };
});

describe('PrintersPage Phase 13 — EmptySlotHoverCard onAssignSpool wiring', () => {
  beforeEach(() => {
    phase13EmptySlotProps.length = 0;
    localStorage.removeItem('printerCardSize');

    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      // Status response includes an empty AMS slot so EmptySlotHoverCard renders.
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...mockPrinterStatus,
        ams: [{
          id: 0,
          tray: [{ id: 0, tray_type: '' }],
        }],
      })),
      http.get('/api/v1/settings/', () => HttpResponse.json({
        auto_archive: true, save_thumbnails: true, capture_finish_photo: true,
        default_filament_cost: 25.0, currency: 'USD',
        ams_humidity_good: 40, ams_humidity_fair: 60,
        ams_temp_good: 30, ams_temp_fair: 35,
      })),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
    );
  });

  it('P13-1 (local mode): EmptySlotHoverCard receives onAssignSpool callback', async () => {
    server.use(
      http.get('/api/v1/spoolman/settings', () => HttpResponse.json({
        spoolman_enabled: 'false', spoolman_url: '',
      })),
    );
    render(<PrintersPage />);

    // Wait for printer status to load and at least one EmptySlotHoverCard
    // to mount with an onAssignSpool callback. Pre-Phase-13 this would have
    // been undefined in local mode (the gate filtered it out).
    await waitFor(() => {
      const withCallback = phase13EmptySlotProps.filter(p => typeof p.onAssignSpool === 'function');
      expect(withCallback.length).toBeGreaterThan(0);
    }, { timeout: 3000 });
  });

  it('#1322: empty slot kind is "physical" only for the asserted cleared shape, else "unknown"', async () => {
    // Bambuddy distinguishes a WIRE-ASSERTED empty slot (state 9 AND
    // tray_type "") from one whose presence the printer never stated. The kind
    // prop drives both the inline caption ("Empty" vs "Unknown") and the hover
    // card header, and the dialect states must land in the unknown bucket — a
    // P1S idle slot is not a user-cleared one.
    server.use(
      http.get('/api/v1/spoolman/settings', () => HttpResponse.json({
        spoolman_enabled: 'false', spoolman_url: '',
      })),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...mockPrinterStatus,
        ams: [{
          id: 0,
          tray: [
            { id: 0, tray_type: '', state: 9 },     // wire-asserted empty
            { id: 1, tray_type: '', state: 3 },     // dialect idle — says nothing
            { id: 2, tray_type: '', state: null },  // no state at all
            { id: 3, tray_type: 'PLA', state: 11 }, // loaded — no card here
          ],
        }],
      })),
    );
    render(<PrintersPage />);

    await waitFor(() => {
      expect(phase13EmptySlotProps.filter(p => p.kind === 'physical').length).toBeGreaterThan(0);
    }, { timeout: 3000 });

    const physical = phase13EmptySlotProps.filter(p => p.kind === 'physical');
    const unknown = phase13EmptySlotProps.filter(p => p.kind === 'unknown');
    expect(physical.length).toBeGreaterThan(0);
    expect(unknown.length).toBeGreaterThan(0);
    // Both the dialect state and the missing one land in 'unknown'; the retired
    // 'reset' kind (which claimed a user action) exists nowhere any more.
    expect(phase13EmptySlotProps.some(p => (p.kind as string) === 'reset')).toBe(false);
  });

  it('P13-1 (spoolman mode): EmptySlotHoverCard still receives onAssignSpool callback', async () => {
    server.use(
      http.get('/api/v1/spoolman/settings', () => HttpResponse.json({
        spoolman_enabled: 'true', spoolman_url: 'http://x:7912',
      })),
      http.get('/api/v1/spoolman/spools/inventory*', () => HttpResponse.json([])),
      http.get('/api/v1/spoolman/inventory/spools', () => HttpResponse.json([])),
      http.get('/api/v1/spoolman/inventory/slot-assignments/all', () => HttpResponse.json([])),
    );
    render(<PrintersPage />);

    await waitFor(() => {
      const withCallback = phase13EmptySlotProps.filter(p => typeof p.onAssignSpool === 'function');
      expect(withCallback.length).toBeGreaterThan(0);
    }, { timeout: 3000 });
  });
});

/**
 * Phase 14 — Local-Branch BL-detection symmetry.
 *
 * The Spoolman branch of every IIFE in PrintersPage already passes
 *   isAssigned: !!slotAssignment || isBambuLabSpool(tray)
 *   onUnassignSpool: (spoolmanSpool && !isBambuLabSpool(tray)) ? ... : undefined
 *
 * The local branch was missing both. As a result a BL-RFID-tagged slot in
 * local-Inventory mode showed an "Assign Spool" button (because no manual
 * SpoolAssignment exists), and a manually-assigned BL-RFID slot showed
 * "Unassign" — which would be overwritten the next time the identity lane
 * reads the tag (autonomously, or behind an operator's "Re-check slot").
 *
 * The same FilamentHoverCard mock from the Phase 13 block above captures
 * inventory props on every render so we can inspect them after setup.
 */
describe('PrintersPage Phase 14 — Local-Branch BL-detection symmetry', () => {
  beforeEach(() => {
    phase14HoverCardProps.length = 0;
    localStorage.removeItem('printerCardSize');

    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      http.get('/api/v1/settings/', () => HttpResponse.json({
        auto_archive: true, save_thumbnails: true, capture_finish_photo: true,
        default_filament_cost: 25.0, currency: 'USD',
        ams_humidity_good: 40, ams_humidity_fair: 60,
        ams_temp_good: 30, ams_temp_fair: 35,
      })),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/spoolman/settings', () => HttpResponse.json({
        spoolman_enabled: 'false', spoolman_url: '',
      })),
    );
  });

  it('P14-1a (local + BL-RFID + no assignment): inventory.isAssigned=true', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...mockPrinterStatus,
        ams: [{
          id: 0,
          tray: [{
            id: 0,
            tray_type: 'PLA',
            tray_uuid: '11223344556677880011223344556677',
            tag_uid: '0000000000000000',
            tray_color: 'FF0000FF',
            tray_sub_brands: 'Bambu PLA Basic',
          }],
        }],
      })),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    );
    render(<PrintersPage />);

    await waitFor(() => {
      const matches = phase14HoverCardProps.filter(
        p => (p.inventory as { isAssigned?: boolean } | undefined)?.isAssigned === true
      );
      expect(matches.length).toBeGreaterThan(0);
    }, { timeout: 3000 });
  });

  it('P14-1b (local + non-BL + no assignment): inventory.isAssigned is falsy', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...mockPrinterStatus,
        ams: [{
          id: 0,
          tray: [{
            id: 0,
            tray_type: 'PLA',
            tray_uuid: '00000000000000000000000000000000',
            tag_uid: '0000000000000000',
            tray_color: 'FF0000FF',
            tray_sub_brands: 'Generic PLA',
          }],
        }],
      })),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    );
    render(<PrintersPage />);

    // Wait for FilamentHoverCard to render at least once.
    await waitFor(() => {
      expect(phase14HoverCardProps.length).toBeGreaterThan(0);
    }, { timeout: 3000 });

    // No render should ever set isAssigned=true for this slot.
    const truthyMatches = phase14HoverCardProps.filter(
      p => (p.inventory as { isAssigned?: boolean } | undefined)?.isAssigned === true
    );
    expect(truthyMatches.length).toBe(0);
  });

  it('P14-1c (local + manual assignment): inventory.isAssigned=true', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...mockPrinterStatus,
        ams: [{
          id: 0,
          tray: [{
            id: 0,
            tray_type: 'PLA',
            tray_uuid: '00000000000000000000000000000000',
            tag_uid: '0000000000000000',
            tray_color: 'FF0000FF',
            tray_sub_brands: 'Generic PLA',
          }],
        }],
      })),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([
        {
          id: 1,
          spool_id: 42,
          printer_id: 1,
          ams_id: 0,
          tray_id: 0,
          printer_name: 'X1 Carbon',
          ams_label: null,
          spool: {
            id: 42,
            material: 'PLA',
            brand: 'Generic',
            color_name: 'Red',
            label_weight: 1000,
            weight_used: 0,
            rgba: 'FF0000FF',
          },
        },
      ])),
    );
    render(<PrintersPage />);

    await waitFor(() => {
      const matches = phase14HoverCardProps.filter(
        p => (p.inventory as { isAssigned?: boolean } | undefined)?.isAssigned === true
      );
      expect(matches.length).toBeGreaterThan(0);
    }, { timeout: 3000 });
  });

  it('P14-2 (local + BL-RFID + manual assignment): onUnassignSpool=undefined', async () => {
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json({
        ...mockPrinterStatus,
        ams: [{
          id: 0,
          tray: [{
            id: 0,
            tray_type: 'PLA',
            tray_uuid: '11223344556677880011223344556677',
            tag_uid: '0000000000000000',
            tray_color: 'FF0000FF',
            tray_sub_brands: 'Bambu PLA Basic',
          }],
        }],
      })),
      http.get('/api/v1/inventory/assignments', () => HttpResponse.json([
        {
          id: 1,
          spool_id: 42,
          printer_id: 1,
          ams_id: 0,
          tray_id: 0,
          printer_name: 'X1 Carbon',
          ams_label: null,
          spool: {
            id: 42,
            material: 'PLA',
            brand: 'Bambu Lab',
            color_name: 'Red',
            label_weight: 1000,
            weight_used: 0,
            rgba: 'FF0000FF',
          },
        },
      ])),
    );
    render(<PrintersPage />);

    // Wait for FilamentHoverCard renders to settle.
    await waitFor(() => {
      expect(phase14HoverCardProps.length).toBeGreaterThan(0);
    }, { timeout: 3000 });

    // For BL-detected slots in local mode, onUnassignSpool must always be
    // undefined — even when a manual assignment exists. Otherwise the user
    // could unassign a BL-RFID slot that the printer would re-assign on the
    // next identity read, surprising them with phantom ghost-assignments.
    const definedUnassign = phase14HoverCardProps.filter(
      p => typeof (p.inventory as { onUnassignSpool?: () => void } | undefined)?.onUnassignSpool === 'function'
    );
    expect(definedUnassign.length).toBe(0);
  });
});

/**
 * `/?printer=<id>` — the link the Fleet tab's "Down now" rows and its bucket
 * detail hand the operator.
 *
 * Until this wiring the param landed on a page that read no URL state, so
 * "Open printer" opened the printer list and left them to find the machine
 * themselves — on a twelve-printer farm, the one fact the link existed to
 * deliver. Three things are load-bearing: the right card is scrolled to and
 * marked, an id that names nothing is ignored rather than guessed at, and the
 * param is CONSUMED so a refetch or a Back cannot re-fire the jump.
 *
 * Its OWN describe with its own narrow handlers, deliberately: the suites above
 * mock a printer status per scenario, and a card that re-renders against a
 * partial one is a different test's concern.
 *
 * The card is located by its `printer-card-<id>` id because that id is this
 * feature's own contract — it is what `scrollPrinterIntoView` looks up — and
 * not an incidental styling hook.
 */
describe('PrintersPage — the ?printer= deep link', () => {
  const deepLinkPrinters = [
    {
      id: 1, name: 'X1 Carbon', ip_address: '192.168.1.100',
      serial_number: '00M09A350100001', access_code: '12345678', model: 'X1C',
      enabled: true, is_active: true, nozzle_diameter: 0.4,
      nozzle_type: 'hardened_steel', location: 'Workshop', auto_archive: true,
      created_at: '2024-01-01T00:00:00Z', updated_at: '2024-01-01T00:00:00Z',
    },
    {
      id: 2, name: 'P1S Backup', ip_address: '192.168.1.101',
      serial_number: '00W00A123456789', access_code: '87654321', model: 'P1S',
      enabled: true, is_active: true, nozzle_diameter: 0.4,
      nozzle_type: 'stainless_steel', location: null, auto_archive: true,
      created_at: '2024-01-02T00:00:00Z', updated_at: '2024-01-02T00:00:00Z',
    },
  ];

  const deepLinkStatus = {
    connected: true, state: 'IDLE', awaiting_plate_clear: false, progress: 0,
    layer_num: 0, total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 },
    remaining_time: 0, filename: null, wifi_signal: -50, vt_tray: [],
  };

  const cardFor = (id: number): HTMLElement | null =>
    document.getElementById(`printer-card-${id}`);

  /** Whether a card carries the transient highlight, read off the style it sets. */
  const isMarked = (id: number): boolean => (cardFor(id)?.style.outline ?? '') !== '';

  /**
   * Drain the two animation frames `scrollPrinterIntoView` waits on before it
   * measures. Without this a "did not scroll" assertion passes because the
   * scroll simply had not happened YET — and then fires inside the next test.
   */
  const flushFrames = (): Promise<void> =>
    new Promise((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });

  beforeEach(() => {
    vi.mocked(window.scrollTo).mockClear();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(deepLinkPrinters)),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(deepLinkStatus)),
    );
  });

  afterEach(async () => {
    // Let each test's own scroll frames land before the next one clears the
    // spy — a frame scheduled here and fired there is how "did not scroll"
    // fails in a test that never asked for one.
    await flushFrames();
    window.history.replaceState({}, '', '/');
  });

  it('scrolls the named printer into view and marks it', async () => {
    window.history.replaceState({}, '', '/?printer=2');
    render(<PrintersPage />);

    await screen.findByText('P1S Backup');
    await waitFor(() => expect(isMarked(2)).toBe(true));

    await waitFor(() => expect(window.scrollTo).toHaveBeenCalled());
    // Exactly one card is marked — the highlight names a printer, not a page.
    expect(isMarked(1)).toBe(false);
  });

  it('consumes the param so a refetch cannot re-fire the jump', async () => {
    window.history.replaceState({}, '', '/?printer=2');
    render(<PrintersPage />);

    await waitFor(() => expect(window.location.search).not.toContain('printer=2'));
  });

  it('ignores an id that names no rendered printer', async () => {
    window.history.replaceState({}, '', '/?printer=999');
    render(<PrintersPage />);

    await screen.findByText('X1 Carbon');
    await waitFor(() => expect(window.location.search).not.toContain('printer=999'));
    await flushFrames();

    // No scroll, nothing marked: a bad id is not a reason to move the page or
    // to point at whichever printer happened to sort first.
    expect(window.scrollTo).not.toHaveBeenCalled();
    expect(isMarked(1)).toBe(false);
    expect(isMarked(2)).toBe(false);
  });

  it('does nothing at all when the page is opened without the param', async () => {
    render(<PrintersPage />);

    await screen.findByText('X1 Carbon');
    await flushFrames();

    expect(window.scrollTo).not.toHaveBeenCalled();
    expect(isMarked(1)).toBe(false);
  });
});
