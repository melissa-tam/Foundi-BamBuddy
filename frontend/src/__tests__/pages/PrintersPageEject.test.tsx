/**
 * W2 manual "Eject now" + W3 cooldown tooltip + on-demand "Eject plate…" on the
 * Printers page.
 *
 * - The Eject now button appears beside mark-cleared whenever the plate gate is
 *   raised (same predicate as showClearPlateButton) and the user can control
 *   printers (auth-disabled test → hasPermission returns true).
 * - A `bed_hot` 409 opens the confirm dialog showing BOTH live temps; confirming
 *   re-calls the endpoint with allow_hot=true.
 * - While a cooldown eject watch is armed, the mark-cleared button carries the
 *   "marking clears cancels the pending auto-eject" hint (W3).
 * - The overflow menu's "Eject plate…" declares the plate occupied
 *   (`declare_occupied: true`) and lands in the same foreign-plate confirm, where
 *   the detected part height is editable; cancelling leaves the gate raised.
 * - "Mark plate as occupied" is the disconnected-only lane (an eject needs a
 *   live session), and a confirm-leg failure renders inside the dialog rather
 *   than as a toast.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { PrintersPage } from '../../pages/PrintersPage';

/** Wire shape of the eject request body (client.ts always sends all four). */
interface EjectBody {
  allow_hot: boolean;
  eject_profile_id: number | null;
  declare_occupied: boolean;
  max_z_height_mm: number | null;
}

const printer = {
  id: 1,
  name: 'H2S-Alpha',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'H2S',
  is_active: true,
  auto_archive: true,
  nozzle_count: 1,
  location: 'Farm',
  quarantined: false,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

function statusFinish(extra: Record<string, unknown> = {}) {
  return {
    connected: true,
    state: 'FINISH',
    awaiting_plate_clear: true,
    progress: 0,
    layer_num: 0,
    total_layers: 0,
    temperatures: { nozzle: 25, bed: 25, chamber: 25 },
    remaining_time: 0,
    filename: null,
    wifi_signal: -50,
    vt_tray: [],
    ...extra,
  };
}

/** Connected, idle, gate DOWN — the state the on-demand eject is offered in. */
function statusIdle(extra: Record<string, unknown> = {}) {
  return statusFinish({ state: 'IDLE', awaiting_plate_clear: false, ...extra });
}

async function openActionsMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'More' }));
}

function mount(status: Record<string, unknown>) {
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/settings/', () => HttpResponse.json({ require_plate_clear: true })),
    http.get('/api/v1/settings/ui-preferences', () => HttpResponse.json({ require_plate_clear: true })),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
}

describe('PrintersPage manual eject (W2) + cooldown tooltip (W3)', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize'); // default cardSize 2 → expanded
  });

  it('renders the Eject now button when the plate gate is raised', async () => {
    mount(statusFinish());
    render(<PrintersPage />);
    expect(await screen.findByRole('button', { name: /eject now/i })).toBeInTheDocument();
  });

  it('does not render Eject now when the gate is not raised', async () => {
    mount(statusFinish({ awaiting_plate_clear: false }));
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    expect(screen.queryByRole('button', { name: /eject now/i })).not.toBeInTheDocument();
  });

  it('opens the hot-bed confirm with live temps on a bed_hot 409, then re-calls with allow_hot=true', async () => {
    const ejectCalls: Array<{ allow_hot: boolean }> = [];
    mount(statusFinish());
    server.use(
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        const body = (await request.json()) as { allow_hot: boolean };
        ejectCalls.push(body);
        if (!body.allow_hot) {
          return HttpResponse.json(
            { detail: { code: 'bed_hot', bed_c: 45, threshold_c: 33 } },
            { status: 409 },
          );
        }
        return HttpResponse.json({ mode: 'dispatched' });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);

    const ejectBtn = await screen.findByRole('button', { name: /eject now/i });
    await user.click(ejectBtn);

    // Confirm dialog appears; the body shows BOTH the live bed and threshold.
    expect(await screen.findByText('Eject while bed is hot?')).toBeInTheDocument();
    const body = screen.getByText(/release threshold/i);
    expect(body.textContent).toContain('45');
    expect(body.textContent).toContain('33');
    await waitFor(() => expect(ejectCalls).toHaveLength(1));
    expect(ejectCalls[0].allow_hot).toBe(false);

    // Confirm → re-call with allow_hot=true (dialog confirm carries the same label).
    const ejectButtons = screen.getAllByRole('button', { name: /eject now/i });
    await user.click(ejectButtons[ejectButtons.length - 1]);
    await waitFor(() => expect(ejectCalls).toHaveLength(2));
    expect(ejectCalls[1].allow_hot).toBe(true);
  });

  it('opens the foreign-plate confirm on a foreign_plate 409 with the suggested profile preselected, then ejects with that profile', async () => {
    const ejectCalls: EjectBody[] = [];
    mount(statusFinish());
    server.use(
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([
          { id: 7, name: 'Alpha profile' },
          { id: 9, name: 'Beta profile' },
        ]),
      ),
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        const body = (await request.json()) as EjectBody;
        ejectCalls.push(body);
        if (body.eject_profile_id === null) {
          return HttpResponse.json(
            {
              detail: {
                code: 'foreign_plate',
                message: 'This plate came from a manual Bambu Studio print.',
                print_name: 'manual_widget.3mf',
                max_z_height_mm: 24.6,
                suggested_eject_profile_id: 9,
              },
            },
            { status: 409 },
          );
        }
        return HttpResponse.json({ mode: 'dispatched', queue_item_id: null });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);

    const ejectBtn = await screen.findByRole('button', { name: /eject now/i });
    await user.click(ejectBtn);

    // Foreign-plate dialog opens with the print name + the detected height
    // prefilled into the (editable) part-height field.
    expect(await screen.findByText('Eject a foreign print?')).toBeInTheDocument();
    expect(screen.getByText('manual_widget.3mf')).toBeInTheDocument();
    expect(screen.getByLabelText('Part height (mm)')).toHaveValue(24.6);
    await waitFor(() => expect(ejectCalls).toHaveLength(1));
    expect(ejectCalls[0]).toEqual({
      allow_hot: false,
      eject_profile_id: null,
      declare_occupied: false,
      max_z_height_mm: null,
    });

    // The suggested profile (id 9) is preselected in the picker.
    const select = await screen.findByLabelText('Eject profile');
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe('9'));

    // Confirm → re-call with allow_hot=false + the chosen profile id, and the
    // (unedited) prefilled height as the explicit override.
    const ejectButtons = screen.getAllByRole('button', { name: /eject now/i });
    await user.click(ejectButtons[ejectButtons.length - 1]);
    await waitFor(() => expect(ejectCalls).toHaveLength(2));
    expect(ejectCalls[1]).toEqual({
      allow_hot: false,
      eject_profile_id: 9,
      declare_occupied: false,
      max_z_height_mm: 24.6,
    });
  });

  it('shows the cancels-eject hint on the mark-cleared button while a cooldown watch is armed', async () => {
    mount(statusFinish({ eject_watch: { threshold_c: 33 } }));
    render(<PrintersPage />);
    expect(
      await screen.findByTitle('Marking the plate cleared cancels the pending auto-eject.'),
    ).toBeInTheDocument();
  });
});

describe('on-demand "Eject plate…" (declare-occupied)', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
  });

  const foreignDetail = {
    code: 'foreign_plate',
    message: 'This plate was not dispatched by the farm.',
    print_name: 'orphan_part.3mf',
    max_z_height_mm: 18,
    suggested_eject_profile_id: 7,
  };

  it('offers the item on a connected, idle printer with the gate down', async () => {
    mount(statusIdle());
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(await screen.findByRole('button', { name: /eject plate/i })).toBeInTheDocument();
  });

  it('hides the item when the plate gate is already raised', async () => {
    mount(statusFinish());
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(screen.queryByRole('button', { name: /eject plate/i })).not.toBeInTheDocument();
  });

  it('hides the item while the printer is printing', async () => {
    mount(statusIdle({ state: 'RUNNING' }));
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(screen.queryByRole('button', { name: /eject plate/i })).not.toBeInTheDocument();
  });

  it('hides the item while the printer is disconnected', async () => {
    mount(statusIdle({ connected: false }));
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(screen.queryByRole('button', { name: /eject plate/i })).not.toBeInTheDocument();
  });

  it('declares the plate occupied, prefills the detected height, and sends the corrected value', async () => {
    const ejectCalls: EjectBody[] = [];
    mount(statusIdle());
    server.use(
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 7, name: 'Alpha profile' }]),
      ),
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        const body = (await request.json()) as EjectBody;
        ejectCalls.push(body);
        if (body.eject_profile_id === null) {
          return HttpResponse.json({ detail: foreignDetail }, { status: 409 });
        }
        return HttpResponse.json({ mode: 'dispatched', queue_item_id: null });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    await user.click(await screen.findByRole('button', { name: /eject plate/i }));

    // The declaration rides the very first call — the server raises the gate.
    await waitFor(() => expect(ejectCalls).toHaveLength(1));
    expect(ejectCalls[0]).toEqual({
      allow_hot: false,
      eject_profile_id: null,
      declare_occupied: true,
      max_z_height_mm: null,
    });

    // …and the familiar foreign-plate confirm carries the detected height.
    expect(await screen.findByText('Eject a foreign print?')).toBeInTheDocument();
    expect(screen.getByText('orphan_part.3mf')).toBeInTheDocument();
    const height = screen.getByLabelText('Part height (mm)');
    expect(height).toHaveValue(18);

    // The operator measures the real part and corrects the detected value. An
    // empty height must not reach the backend — `max_z` sets the sweep lift.
    await user.clear(height);
    const confirmWhileBlank = screen.getAllByRole('button', { name: /eject now/i });
    expect(confirmWhileBlank[confirmWhileBlank.length - 1]).toBeDisabled();
    await user.type(height, '25');

    const ejectButtons = screen.getAllByRole('button', { name: /eject now/i });
    await user.click(ejectButtons[ejectButtons.length - 1]);
    await waitFor(() => expect(ejectCalls).toHaveLength(2));
    expect(ejectCalls[1]).toEqual({
      allow_hot: false,
      eject_profile_id: 7,
      declare_occupied: true,
      max_z_height_mm: 25,
    });
  });

  it('leaves the gate raised when the operator cancels the confirm', async () => {
    // The server raises the gate before it 409s and never rolls it back, so the
    // status endpoint reports it from the eject call onwards.
    let gateRaised = false;
    mount(statusIdle());
    server.use(
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json(statusIdle({ awaiting_plate_clear: gateRaised })),
      ),
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 7, name: 'Alpha profile' }]),
      ),
      http.post('/api/v1/printers/:id/eject', () => {
        gateRaised = true;
        return HttpResponse.json({ detail: foreignDetail }, { status: 409 });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    await user.click(await screen.findByRole('button', { name: /eject plate/i }));
    expect(await screen.findByText('Eject a foreign print?')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() =>
      expect(screen.queryByText('Eject a foreign print?')).not.toBeInTheDocument(),
    );

    // The plate IS occupied: the card now offers the gate's own affordances,
    // and mark-cleared is the undo.
    expect(
      await screen.findByRole('button', { name: /mark plate as cleared/i }),
    ).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /eject now/i })).toBeInTheDocument();
  });
});

describe('"Mark plate as occupied" (disconnected lane)', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
  });

  it('offers the item on a disconnected printer with the gate down', async () => {
    mount(statusIdle({ connected: false }));
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(
      await screen.findByRole('button', { name: /mark plate as occupied/i }),
    ).toBeInTheDocument();
  });

  it('hides the item while the printer is connected', async () => {
    mount(statusIdle());
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(
      screen.queryByRole('button', { name: /mark plate as occupied/i }),
    ).not.toBeInTheDocument();
  });

  it('hides the item when the disconnected printer already reports the gate', async () => {
    mount(statusIdle({ connected: false, awaiting_plate_clear: true }));
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(
      screen.queryByRole('button', { name: /mark plate as occupied/i }),
    ).not.toBeInTheDocument();
  });

  it('posts the declaration and confirms it with a toast', async () => {
    let posted = 0;
    mount(statusIdle({ connected: false }));
    server.use(
      http.post('/api/v1/printers/:id/mark-plate-occupied', () => {
        posted += 1;
        return HttpResponse.json({ success: true, message: 'Plate marked as occupied' });
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    await user.click(await screen.findByRole('button', { name: /mark plate as occupied/i }));

    await waitFor(() => expect(posted).toBe(1));
    expect(
      await screen.findByText('Plate marked as occupied — dispatch blocked until cleared'),
    ).toBeInTheDocument();
  });
});

describe('foreign-eject confirm-leg failures', () => {
  beforeEach(() => {
    localStorage.removeItem('printerCardSize');
  });

  it('renders the failure inside the still-open dialog instead of toasting it', async () => {
    mount(statusFinish());
    server.use(
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 7, name: 'Alpha profile' }]),
      ),
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        const body = (await request.json()) as EjectBody;
        if (body.eject_profile_id === null) {
          return HttpResponse.json(
            {
              detail: {
                code: 'foreign_plate',
                message: 'This plate was not dispatched by the farm.',
                print_name: 'orphan_part.3mf',
                max_z_height_mm: 12,
                suggested_eject_profile_id: 7,
              },
            },
            { status: 409 },
          );
        }
        // Plain-string 409 detail — the generator's own part-height guard.
        return HttpResponse.json(
          { detail: 'Part height 12.0 mm exceeds the profile limit of 10.0 mm' },
          { status: 409 },
        );
      }),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);
    await user.click(await screen.findByRole('button', { name: /eject now/i }));
    expect(await screen.findByText('Eject a foreign print?')).toBeInTheDocument();

    const ejectButtons = screen.getAllByRole('button', { name: /eject now/i });
    await user.click(ejectButtons[ejectButtons.length - 1]);

    const dialog = await screen.findByRole('dialog');
    expect(
      await within(dialog).findByText(/exceeds the profile limit of 10\.0 mm/i),
    ).toBeInTheDocument();
    // The dialog stays open so the height can be corrected and retried…
    expect(within(dialog).getByText('Eject a foreign print?')).toBeInTheDocument();
    // …and the message is rendered exactly once (no duplicate toast).
    expect(screen.getAllByText(/exceeds the profile limit of 10\.0 mm/i)).toHaveLength(1);
  });
});
