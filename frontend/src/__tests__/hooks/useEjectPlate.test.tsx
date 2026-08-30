/**
 * `useEjectPlate` — the routing half of the one eject flow.
 *
 * Every door on a printer card calls the same `eject()`, so the hook is where
 * the backend's answer is turned into exactly one of three outcomes:
 *
 *   dialog       — a plate that needs operator input (`foreign_plate`). An
 *                  unrecognized plate must NEVER dead-end in a toast.
 *   hot confirm  — `bed_hot`, carrying profile/height/declaration through the
 *                  re-call so the confirmed sweep is the one the operator set up.
 *   toast        — every other refusal, in sys-admin copy keyed off the code
 *                  (including the pre-authority spellings the current backend
 *                  still emits) rather than raw backend English.
 *
 * Pinned here rather than through the page because the page renders one of
 * these per card and the routing table is the thing that must be exhaustive.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { useEjectPlate } from '../../hooks/useEjectPlate';

/** Wire shape of the eject request body (client.ts always sends all four). */
interface EjectBody {
  allow_hot: boolean;
  eject_profile_id: number | null;
  declare_occupied: boolean;
  max_z_height_mm: number | null;
}

const PRINTER_ID = 4;

/** Minimal consumer: one button per leg, the hook's state rendered as text. */
function Harness() {
  const eject = useEjectPlate(PRINTER_ID);
  return (
    <div>
      <button type="button" onClick={() => eject.eject({ declareOccupied: true })}>
        door
      </button>
      <button type="button" onClick={eject.confirmDialog}>
        confirm-dialog
      </button>
      <button type="button" onClick={eject.confirmHot}>
        confirm-hot
      </button>
      <button type="button" onClick={() => eject.setHeightInput('40')}>
        set-height
      </button>
      <button type="button" onClick={() => eject.setHeightInput('')}>
        clear-height
      </button>
      <span data-testid="origin">{eject.dialog ? eject.dialog.origin : 'closed'}</span>
      <span data-testid="print-name">{eject.dialog?.printName ?? ''}</span>
      <span data-testid="suggested">{String(eject.dialog?.suggestedEjectProfileId ?? '')}</span>
      <span data-testid="height">{eject.heightInput}</span>
      <span data-testid="height-valid">{String(eject.heightValid)}</span>
      <span data-testid="hot">
        {eject.hotConfirm ? `${eject.hotConfirm.bedC}/${eject.hotConfirm.thresholdC}` : 'closed'}
      </span>
      <span data-testid="dialog-error">{eject.dialogError ?? ''}</span>
    </div>
  );
}

/** Answer the eject POST with one structured refusal, recording every body. */
function refuseWith(detail: Record<string, unknown>, status = 409): EjectBody[] {
  const calls: EjectBody[] = [];
  server.use(
    http.post('/api/v1/printers/:id/eject', async ({ request }) => {
      calls.push((await request.json()) as EjectBody);
      return HttpResponse.json({ detail }, { status });
    }),
  );
  return calls;
}

async function clickDoor() {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole('button', { name: 'door' }));
  return user;
}

describe('useEjectPlate — foreign_plate opens the dialog', () => {
  const foreign = {
    code: 'foreign_plate',
    message: 'This plate was not dispatched by the farm.',
    origin: 'foreign',
    print_name: 'orphan_part.3mf',
    max_z_height_mm: 18.44,
    suggested_eject_profile_id: 7,
  };

  it('opens with the origin, print name, suggestion and a 0.1-rounded height', async () => {
    const calls = refuseWith(foreign);
    await clickDoor();

    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('foreign'));
    expect(screen.getByTestId('print-name')).toHaveTextContent('orphan_part.3mf');
    expect(screen.getByTestId('suggested')).toHaveTextContent('7');
    // Prefilled at the field's own precision so it never opens on a float
    // artifact the operator would have to retype.
    expect(screen.getByTestId('height')).toHaveTextContent('18.4');
    expect(screen.getByTestId('height-valid')).toHaveTextContent('true');
    expect(calls[0].declare_occupied).toBe(true);
  });

  it('carries a farm_unit origin through', async () => {
    refuseWith({ ...foreign, origin: 'farm_unit', print_name: 'SKU007.01 (#2656-20)' });
    await clickDoor();
    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('farm_unit'));
  });

  it('carries a declared origin through', async () => {
    refuseWith({ ...foreign, origin: 'declared', print_name: null });
    await clickDoor();
    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('declared'));
    expect(screen.getByTestId('print-name')).toHaveTextContent('');
  });

  it('falls back to foreign when the backend omits origin', async () => {
    // A backend predating the field: a plate the farm cannot attribute IS
    // foreign, so that is the safe read.
    refuseWith({
      code: 'foreign_plate',
      print_name: 'orphan_part.3mf',
      max_z_height_mm: 18.44,
      suggested_eject_profile_id: 7,
    });
    await clickDoor();
    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('foreign'));
  });

  it('leaves the height blank and blocks the confirm when no height was detected', async () => {
    const calls = refuseWith({ ...foreign, max_z_height_mm: null });
    const user = await clickDoor();

    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('foreign'));
    expect(screen.getByTestId('height')).toHaveTextContent('');
    expect(screen.getByTestId('height-valid')).toHaveTextContent('false');

    // A blank height must not reach the backend — `max_z` sets the sweep
    // clearance and lift.
    await user.click(screen.getByRole('button', { name: 'confirm-dialog' }));
    await waitFor(() => expect(calls).toHaveLength(1));

    await user.click(screen.getByRole('button', { name: 'set-height' }));
    await waitFor(() => expect(screen.getByTestId('height-valid')).toHaveTextContent('true'));
    await user.click(screen.getByRole('button', { name: 'confirm-dialog' }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual({
      allow_hot: false,
      eject_profile_id: 7,
      declare_occupied: true,
      max_z_height_mm: 40,
    });
  });

  it('blocks the confirm again when the operator clears an already-valid height', async () => {
    const calls = refuseWith(foreign);
    const user = await clickDoor();
    await waitFor(() => expect(screen.getByTestId('height-valid')).toHaveTextContent('true'));

    await user.click(screen.getByRole('button', { name: 'clear-height' }));
    await waitFor(() => expect(screen.getByTestId('height-valid')).toHaveTextContent('false'));
    await user.click(screen.getByRole('button', { name: 'confirm-dialog' }));
    await waitFor(() => expect(calls).toHaveLength(1));
  });

  it('renders a confirm-leg failure inside the dialog instead of toasting it', async () => {
    let call = 0;
    server.use(
      http.post('/api/v1/printers/:id/eject', () => {
        call += 1;
        if (call === 1) return HttpResponse.json({ detail: foreign }, { status: 409 });
        // Plain-string detail: the generator's own part-height guard. It IS
        // the useful sentence, so it survives verbatim.
        return HttpResponse.json(
          { detail: 'Part height 40.0 mm exceeds the profile limit of 10.0 mm' },
          { status: 409 },
        );
      }),
    );
    const user = await clickDoor();

    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('foreign'));
    await user.click(screen.getByRole('button', { name: 'confirm-dialog' }));

    await waitFor(() =>
      expect(screen.getByTestId('dialog-error')).toHaveTextContent(
        'exceeds the profile limit of 10.0 mm',
      ),
    );
    // The dialog stays open so the height can be corrected and retried, and the
    // message is not duplicated as a toast.
    expect(screen.getByTestId('origin')).toHaveTextContent('foreign');
    expect(screen.queryAllByText(/exceeds the profile limit/)).toHaveLength(1);
  });
});

describe('useEjectPlate — bed_hot opens the hot confirm', () => {
  it('carries the profile, height and declaration into the confirmed re-call', async () => {
    let call = 0;
    const calls: EjectBody[] = [];
    server.use(
      http.post('/api/v1/printers/:id/eject', async ({ request }) => {
        calls.push((await request.json()) as EjectBody);
        call += 1;
        if (call === 1) {
          return HttpResponse.json(
            {
              detail: {
                code: 'foreign_plate',
                origin: 'foreign',
                print_name: 'p.3mf',
                max_z_height_mm: 12,
                suggested_eject_profile_id: 3,
              },
            },
            { status: 409 },
          );
        }
        if (call === 2) {
          return HttpResponse.json(
            { detail: { code: 'bed_hot', bed_c: 45.4, threshold_c: 33 } },
            { status: 409 },
          );
        }
        return HttpResponse.json({ mode: 'dispatched', queue_item_id: null });
      }),
    );

    const user = await clickDoor();
    await waitFor(() => expect(screen.getByTestId('origin')).toHaveTextContent('foreign'));
    await user.click(screen.getByRole('button', { name: 'confirm-dialog' }));

    // The hot confirm takes over from the dialog, keeping the live temps.
    await waitFor(() => expect(screen.getByTestId('hot')).toHaveTextContent('45.4/33'));
    expect(screen.getByTestId('origin')).toHaveTextContent('closed');

    await user.click(screen.getByRole('button', { name: 'confirm-hot' }));
    await waitFor(() => expect(calls).toHaveLength(3));
    expect(calls[2]).toEqual({
      allow_hot: true,
      eject_profile_id: 3,
      declare_occupied: true,
      max_z_height_mm: 12,
    });
    // Success closes both and reports it once.
    expect(await screen.findByText('Eject started')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('hot')).toHaveTextContent('closed'));
  });

  it('toasts instead of re-opening the confirm when the temps are unusable', async () => {
    refuseWith({ code: 'bed_hot', bed_c: null, threshold_c: null });
    await clickDoor();
    // No usable temps to show — falls through to the generic failure rather
    // than opening a confirm that cannot state the numbers.
    expect(await screen.findByText('Failed to send command')).toBeInTheDocument();
    expect(screen.getByTestId('hot')).toHaveTextContent('closed');
  });
});

describe('useEjectPlate — every other refusal is an i18n-keyed toast', () => {
  const cases: Array<[string, Record<string, unknown>, string]> = [
    ['job_active', { code: 'job_active' }, 'Printer is running a job. Wait for it to finish or stop it, then eject.'],
    ['printer_busy (legacy)', { code: 'printer_busy' }, 'Printer is running a job. Wait for it to finish or stop it, then eject.'],
    ['dispatch_in_flight', { code: 'dispatch_in_flight' }, 'A queued unit is being sent to this printer. Retry in a few seconds.'],
    ['eject_in_flight pending', { code: 'eject_in_flight', started: false, age_s: 12.4 }, 'Eject sent 12 s ago; waiting for the printer to start it.'],
    ['eject_in_flight started', { code: 'eject_in_flight', started: true, age_s: 61 }, 'Eject running for 61 s.'],
    ['eject_in_flight pending, no age', { code: 'eject_in_flight', started: false, age_s: null }, 'Eject already sent; waiting for the printer to start it.'],
    ['eject_in_flight started, no age', { code: 'eject_in_flight', started: true, age_s: null }, 'Eject running.'],
    ['eject_in_flight (legacy, no extras)', { code: 'eject_in_flight' }, 'Eject already sent; waiting for the printer to start it.'],
    ['bed_unreadable', { code: 'bed_unreadable' }, 'Bed temperature unavailable. Retry in a few seconds.'],
    ['no_donor', { code: 'no_donor' }, 'No file available to build the eject. Mark plate as cleared and remove the part by hand.'],
    ['no_eligible_unit (legacy)', { code: 'no_eligible_unit' }, 'No file available to build the eject. Mark plate as cleared and remove the part by hand.'],
    ['first_article', { code: 'first_article' }, 'First article awaiting approval. Approve or reject it from the run.'],
    ['not_connected', { code: 'not_connected' }, 'Printer is offline. Connect it, then eject.'],
    ['eject_dispatch_failed', { code: 'eject_dispatch_failed', message: 'MQTT publish refused' }, 'Eject command failed: MQTT publish refused'],
    ['profile_not_found', { code: 'profile_not_found' }, 'Eject profile no longer exists. Pick another.'],
    ['no_plate_gate', { code: 'no_plate_gate' }, 'Plate is marked clear. Use Eject plate to declare it occupied.'],
  ];

  it.each(cases)('%s', async (_name, detail, expected) => {
    refuseWith(detail);
    await clickDoor();
    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.getByTestId('origin')).toHaveTextContent('closed');
  });

  it('falls back to the generic failure for a code this build does not know', async () => {
    // Its `message` is backend English (or a bare "HTTP 409") — neither belongs
    // on screen.
    refuseWith({ code: 'some_future_code', message: 'Printer is in a weird state' });
    await clickDoor();
    expect(await screen.findByText('Failed to send command')).toBeInTheDocument();
    expect(screen.queryByText('Printer is in a weird state')).not.toBeInTheDocument();
  });

  it('keeps a plain-string refusal verbatim (no code to key off)', async () => {
    server.use(
      http.post('/api/v1/printers/:id/eject', () =>
        HttpResponse.json({ detail: 'Eject profile has no sweep lanes' }, { status: 409 }),
      ),
    );
    await clickDoor();
    expect(await screen.findByText('Eject profile has no sweep lanes')).toBeInTheDocument();
  });
});

describe('useEjectPlate — success', () => {
  it('reports a dispatched eject once', async () => {
    server.use(
      http.post('/api/v1/printers/:id/eject', () =>
        HttpResponse.json({ mode: 'dispatched', queue_item_id: 51 }),
      ),
    );
    await clickDoor();
    expect(await screen.findByText('Eject started')).toBeInTheDocument();
  });

  it('reports a released cooldown watch with the same sentence', async () => {
    server.use(
      http.post('/api/v1/printers/:id/eject', () =>
        HttpResponse.json({ mode: 'released_watch', queue_item_id: null }),
      ),
    );
    await clickDoor();
    expect(await screen.findByText('Eject started')).toBeInTheDocument();
  });
});
