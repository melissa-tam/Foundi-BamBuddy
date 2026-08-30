/**
 * One eject flow, three doors, on the Printers page.
 *
 * The operator can eject from the overflow menu, the expanded card's raised-gate
 * banner, or the compact card's icon — and all three are the SAME action:
 * available whenever the printer is connected and not running a job, gate up or
 * gate down, sending `declare_occupied: true` so a plate the farm never gated
 * (a print that finished during maintenance, a Bambu Studio LAN print) is
 * ejectable at all. They carry ONE name, "Eject plate", with no ellipsis —
 * gate-up on a farm-known unit the sweep dispatches immediately, so an ellipsis
 * would promise a dialog that may not come.
 *
 * Also pinned here: a `bed_hot` 409 opens the confirm showing BOTH live temps;
 * a `foreign_plate` 409 opens the eject dialog with an editable part height (an
 * unrecognized plate must never dead-end in a toast); cancelling that dialog
 * leaves the gate raised, because the plate IS occupied and "Mark plate as
 * cleared" is the undo; "Mark plate as occupied" stays the disconnected-only
 * lane, since an eject needs a live session; and a confirm-leg failure renders
 * inside the dialog rather than as a toast.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
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

/** The one action name every door carries. */
const EJECT_ACTION = 'Eject plate';
/** The confirm button inside either dialog — deliberately a different word. */
const EJECT_CONFIRM = 'Eject now';
/** Title of the eject dialog for a plate the farm never dispatched. */
const FOREIGN_TITLE = 'Eject plate — print not dispatched by the farm';

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

/** Connected, idle, gate DOWN — the on-demand door's state. */
function statusIdle(extra: Record<string, unknown> = {}) {
  return statusFinish({ state: 'IDLE', awaiting_plate_clear: false, ...extra });
}

/**
 * Choose the card layout. The page reads `printerCardSize` (1 = compact,
 * anything else = expanded) at mount, and the suite replaces `localStorage`
 * with bare `vi.fn()`s — so the size has to be stubbed on the READ. Writing it
 * is a silent no-op that leaves every card expanded.
 */
function stubCardSize(size: '1' | '2') {
  vi.mocked(localStorage.getItem).mockImplementation((key: string) =>
    key === 'printerCardSize' ? size : null,
  );
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

/** Record every eject body; answer `detail` on the first call and 200 after. */
function ejectHandler(detail: Record<string, unknown> | null) {
  const calls: EjectBody[] = [];
  server.use(
    http.post('/api/v1/printers/:id/eject', async ({ request }) => {
      const body = (await request.json()) as EjectBody;
      calls.push(body);
      if (detail && body.eject_profile_id === null) {
        return HttpResponse.json({ detail }, { status: 409 });
      }
      return HttpResponse.json({ mode: 'dispatched', queue_item_id: null });
    }),
  );
  return calls;
}

const foreignDetail = {
  code: 'foreign_plate',
  message: 'This plate was not dispatched by the farm.',
  origin: 'foreign',
  print_name: 'orphan_part.3mf',
  max_z_height_mm: 18,
  suggested_eject_profile_id: 7,
};

/** The eject dialog's confirm (the last "Eject now" on screen). */
function dialogConfirm() {
  return within(screen.getByRole('dialog')).getByRole('button', { name: EJECT_CONFIRM });
}

describe('door 2 — the expanded card\'s raised-gate banner', () => {
  beforeEach(() => {
    stubCardSize('2');
  });

  it('renders the eject button under the one action name when the gate is raised', async () => {
    mount(statusFinish());
    render(<PrintersPage />);
    expect(await screen.findByRole('button', { name: EJECT_ACTION })).toBeInTheDocument();
  });

  it('does not render it when the gate is not raised', async () => {
    mount(statusFinish({ awaiting_plate_clear: false }));
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    // The banner is gone with the gate; the overflow menu (closed here) is the
    // gate-down door.
    expect(screen.queryByRole('button', { name: EJECT_ACTION })).not.toBeInTheDocument();
  });

  it.each(['PREPARE', 'SLICING'])(
    'is withheld in %s while mark-cleared stays — a job owns the plate',
    async (state) => {
      mount(statusFinish({ state }));
      render(<PrintersPage />);
      expect(
        await screen.findByRole('button', { name: /mark plate as cleared/i }),
      ).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: EJECT_ACTION })).not.toBeInTheDocument();
    },
  );

  it('declares the plate occupied like every other door', async () => {
    mount(statusFinish());
    const calls = ejectHandler(null);
    const user = userEvent.setup();
    render(<PrintersPage />);

    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      allow_hot: false,
      eject_profile_id: null,
      declare_occupied: true,
      max_z_height_mm: null,
    });
    expect(await screen.findByText('Eject started')).toBeInTheDocument();
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
    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));

    expect(await screen.findByText('Eject while bed is hot?')).toBeInTheDocument();
    const body = screen.getByText(/release threshold/i);
    expect(body.textContent).toContain('45');
    expect(body.textContent).toContain('33');
    await waitFor(() => expect(ejectCalls).toHaveLength(1));
    expect(ejectCalls[0].allow_hot).toBe(false);

    await user.click(screen.getByRole('button', { name: EJECT_CONFIRM }));
    await waitFor(() => expect(ejectCalls).toHaveLength(2));
    expect(ejectCalls[1].allow_hot).toBe(true);
  });

  it('opens the eject dialog on a foreign_plate 409 with the suggested profile preselected, then ejects with it', async () => {
    mount(statusFinish());
    server.use(
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([
          { id: 7, name: 'Alpha profile' },
          { id: 9, name: 'Beta profile' },
        ]),
      ),
    );
    const calls = ejectHandler({ ...foreignDetail, print_name: 'manual_widget.3mf', max_z_height_mm: 24.6, suggested_eject_profile_id: 9 });

    const user = userEvent.setup();
    render(<PrintersPage />);
    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));

    // The dialog names WHICH plate this is and prefills the detected height.
    expect(await screen.findByRole('dialog', { name: FOREIGN_TITLE })).toBeInTheDocument();
    expect(screen.getByText('manual_widget.3mf')).toBeInTheDocument();
    expect(screen.getByLabelText('Part height (mm)')).toHaveValue(24.6);
    await waitFor(() => expect(calls).toHaveLength(1));

    const select = await screen.findByLabelText('Eject profile');
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe('9'));

    await user.click(dialogConfirm());
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual({
      allow_hot: false,
      eject_profile_id: 9,
      declare_occupied: true,
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

describe('door 1 — the overflow menu item', () => {
  beforeEach(() => {
    stubCardSize('2');
  });

  it('is offered on a connected, idle printer with the gate down', async () => {
    mount(statusIdle());
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(await screen.findByRole('button', { name: EJECT_ACTION })).toBeInTheDocument();
  });

  it('is offered with the gate ALREADY raised — one flow, not two', async () => {
    // The gate-up case used to hide this item, which left the menu unable to
    // reach the eject at the very moment a plate is known to be occupied.
    mount(statusFinish());
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    // The banner door and the menu door, both under the same name.
    await waitFor(() =>
      expect(screen.getAllByRole('button', { name: EJECT_ACTION })).toHaveLength(2),
    );
  });

  it.each(['RUNNING', 'PAUSE', 'PREPARE', 'SLICING'])(
    'is hidden while the printer is in %s',
    async (state) => {
      mount(statusIdle({ state }));
      const user = userEvent.setup();
      render(<PrintersPage />);
      await screen.findByText('H2S-Alpha');
      await openActionsMenu(user);
      expect(screen.queryByRole('button', { name: EJECT_ACTION })).not.toBeInTheDocument();
    },
  );

  it('is hidden while the printer is disconnected', async () => {
    mount(statusIdle({ connected: false }));
    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    expect(screen.queryByRole('button', { name: EJECT_ACTION })).not.toBeInTheDocument();
  });

  it('declares the plate occupied, prefills the detected height, and sends the corrected value', async () => {
    mount(statusIdle());
    server.use(
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 7, name: 'Alpha profile' }]),
      ),
    );
    const calls = ejectHandler(foreignDetail);

    const user = userEvent.setup();
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    await openActionsMenu(user);
    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));

    // The declaration rides the very first call — the server raises the gate.
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      allow_hot: false,
      eject_profile_id: null,
      declare_occupied: true,
      max_z_height_mm: null,
    });

    expect(await screen.findByRole('dialog', { name: FOREIGN_TITLE })).toBeInTheDocument();
    expect(screen.getByText('orphan_part.3mf')).toBeInTheDocument();
    const height = screen.getByLabelText('Part height (mm)');
    expect(height).toHaveValue(18);

    // The operator measures the real part and corrects the detected value. An
    // empty height must not reach the backend — `max_z` sets the sweep lift.
    await user.clear(height);
    expect(dialogConfirm()).toBeDisabled();
    expect(
      screen.getByText('Part height unknown. Enter the tallest point of the part on the plate.'),
    ).toBeInTheDocument();
    await user.type(height, '25');

    await user.click(dialogConfirm());
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual({
      allow_hot: false,
      eject_profile_id: 7,
      declare_occupied: true,
      max_z_height_mm: 25,
    });
  });

  it('leaves the gate raised when the operator cancels the dialog', async () => {
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
    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));
    expect(await screen.findByRole('dialog', { name: FOREIGN_TITLE })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // The plate IS occupied: the card now offers the gate's own affordances,
    // and mark-cleared is the undo.
    expect(
      await screen.findByRole('button', { name: /mark plate as cleared/i }),
    ).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: EJECT_ACTION })).toBeInTheDocument();
  });
});

describe('door 3 — the compact card icon', () => {
  beforeEach(() => {
    stubCardSize('1');
  });

  it('renders with the gate raised', async () => {
    mount(statusFinish());
    render(<PrintersPage />);
    expect(await screen.findByRole('button', { name: EJECT_ACTION })).toBeInTheDocument();
  });

  it('renders with the gate DOWN — the compact card has no overflow menu', async () => {
    mount(statusIdle());
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    expect(screen.queryByRole('button', { name: 'More' })).not.toBeInTheDocument();
    expect(await screen.findByRole('button', { name: EJECT_ACTION })).toBeInTheDocument();
  });

  it('declares the plate occupied from the gate-down icon', async () => {
    mount(statusIdle());
    const calls = ejectHandler(null);
    const user = userEvent.setup();
    render(<PrintersPage />);

    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].declare_occupied).toBe(true);
  });

  it('is hidden while a job owns the plate', async () => {
    mount(statusIdle({ state: 'PREPARE' }));
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    expect(screen.queryByRole('button', { name: EJECT_ACTION })).not.toBeInTheDocument();
  });

  it('is hidden while the printer is disconnected', async () => {
    mount(statusIdle({ connected: false }));
    render(<PrintersPage />);
    await screen.findByText('H2S-Alpha');
    expect(screen.queryByRole('button', { name: EJECT_ACTION })).not.toBeInTheDocument();
  });
});

describe('"Mark plate as occupied" (disconnected lane)', () => {
  beforeEach(() => {
    stubCardSize('2');
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

describe('refusals', () => {
  beforeEach(() => {
    stubCardSize('2');
  });

  it('renders a confirm-leg failure inside the still-open dialog instead of toasting it', async () => {
    mount(statusFinish());
    server.use(
      http.get('/api/v1/eject-profiles', () =>
        HttpResponse.json([{ id: 7, name: 'Alpha profile' }]),
      ),
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        const body = (await request.json()) as EjectBody;
        if (body.eject_profile_id === null) {
          return HttpResponse.json(
            { detail: { ...foreignDetail, max_z_height_mm: 12 } },
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
    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));
    expect(await screen.findByRole('dialog', { name: FOREIGN_TITLE })).toBeInTheDocument();

    await user.click(dialogConfirm());

    const dialog = await screen.findByRole('dialog');
    expect(
      await within(dialog).findByText(/exceeds the profile limit of 10\.0 mm/i),
    ).toBeInTheDocument();
    // The dialog stays open so the height can be corrected and retried…
    expect(within(dialog).getByText(FOREIGN_TITLE)).toBeInTheDocument();
    // …and the message is rendered exactly once (no duplicate toast).
    expect(screen.getAllByText(/exceeds the profile limit of 10\.0 mm/i)).toHaveLength(1);
  });

  it('states a busy printer in operator language rather than raw backend English', async () => {
    mount(statusFinish());
    server.use(
      http.post('/api/v1/printers/:id/eject', () =>
        HttpResponse.json(
          { detail: { code: 'job_active', message: 'Printer busy: RUNNING' } },
          { status: 409 },
        ),
      ),
    );

    const user = userEvent.setup();
    render(<PrintersPage />);
    await user.click(await screen.findByRole('button', { name: EJECT_ACTION }));

    expect(
      await screen.findByText(
        'Printer is running a job. Wait for it to finish or stop it, then eject.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('Printer busy: RUNNING')).not.toBeInTheDocument();
  });
});
