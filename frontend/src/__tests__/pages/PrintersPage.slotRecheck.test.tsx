/**
 * WS11 — "Re-check slot" (doctrine rule 12, incident shape 32).
 *
 * The control this replaces ("Re-read RFID") could not conclude anything on a
 * tagless slot and said nothing when it didn't: an operator clicked it for 21
 * minutes against silence. So the load-bearing assertion in this file is not
 * that the happy path works — it is that EVERY verdict, including the two that
 * change nothing (`unchanged`, `empty`), puts a sentence on screen.
 *
 * Covered here:
 *   - the six verdict sentences, asserted as literal text;
 *   - the acknowledgement's undo action on a `minted` verdict;
 *   - the same undo on the slot card itself, driven by the backend's derived
 *     `recheck_undo_available`: the toast carries the same action but is
 *     dismissible and gone on navigation, so the slot card is the offer's
 *     durable home — and it must be reachable by keyboard, since that card is
 *     where every slot action lives. Also its disappearance once the undo lands,
 *     and its lapse when the backend withdraws the offer underneath the card.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const printer = {
  id: 1,
  name: 'H2S-Alpha',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'H2S',
  enabled: true,
  is_active: true,
  nozzle_diameter: 0.6,
  nozzle_type: 'hardened_steel',
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

/** IDLE, nothing fed (tray_now omitted) and no dry cycle: the re-check verb is
 *  offered rather than blocked by the disabled-reason ladder. */
const status = {
  connected: true,
  state: 'IDLE',
  awaiting_plate_clear: false,
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
  ams_status_main: 0,
  ams: [
    {
      id: 0,
      tray: [
        { id: 0, tray_type: 'PLA', tray_color: 'FF0000FF', tray_sub_brands: 'PLA Basic', state: 10 },
        { id: 1, tray_type: 'PETG', tray_color: '00FF00FF', tray_sub_brands: 'PETG HF', state: 10 },
      ],
    },
  ],
};

/** Mid-print, with slot 1 feeding: the two conditions (`state === 'RUNNING'` and
 *  `tray_now !== 255`) that used to disable "Re-check slot" outright. The
 *  backend takes the click in both — it records a durable intent and answers
 *  `queued` — so disabling the button made the queued path unreachable. */
const printingStatus = { ...status, state: 'RUNNING', tray_now: 0 };

/** The AMS holds a hard write-lockout for the whole dry cycle: this is one of
 *  the reasons that DOES still refuse the click. */
const dryingStatus = { ...status, ams: [{ ...status.ams[0], dry_time: 30 }] };

/** The slot under test: AMS 0, tray 1 — "Slot 2" everywhere in the UI. */
const AMS_ID = 0;
const TRAY_ID = 1;
const SLOT_LABEL = TRAY_ID + 1;

/** The pending sentence, asserted as literal text — it is both the verb's label
 *  and the card's own state line, and the two must stay the same sentence. */
const PENDING = 'Re-check pending. Concludes when the print finishes.';

function recheckResponse(overrides: Record<string, unknown>) {
  return {
    verdict: 'unchanged',
    printer_id: printer.id,
    ams_id: AMS_ID,
    tray_id: TRAY_ID,
    spool_id: null,
    label_weight_g: null,
    brand: null,
    material: null,
    // A read DID go out for this click. The refusal case (drying, an identify in
    // flight, engaged filament) is its own verdict sentence and its own test —
    // defaulting it true here keeps every other case asserting the plain copy.
    read_issued: true,
    undo_available: false,
    ...overrides,
  };
}

function assignmentRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    spool_id: 42,
    printer_id: printer.id,
    printer_name: printer.name,
    ams_id: AMS_ID,
    tray_id: TRAY_ID,
    fingerprint_color: null,
    fingerprint_type: null,
    created_at: '2024-01-01T00:00:00Z',
    spool: {
      id: 42,
      material: 'PETG',
      brand: 'Foundi',
      color_name: 'Green',
      label_weight: 1000,
      weight_used: 120,
      rgba: '00FF00FF',
    },
    ...overrides,
  };
}

/** Close whatever card is open, so the next `openSlotCard` can't return it.
 *  The card is a non-modal `role="dialog"` now, which is the honest way to ask
 *  whether one is on screen. */
async function closeSlotCard(index: number) {
  const slots = await screen.findAllByTestId('filament-slot');
  fireEvent.mouseLeave(slots[index]);
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
}

/** Open the hover card over a slot and return a scope for its contents.
 *  Addressed by role: the card is a non-modal `role="dialog"` now, and the
 *  toast viewport shares its `.fixed.z-[60]` positioning classes, so a
 *  class-based query silently returns a toast once one is on screen. */
async function openSlotCard(index: number) {
  const slots = await screen.findAllByTestId('filament-slot');
  fireEvent.mouseEnter(slots[index]);
  // The card waits 80 ms before showing, to swallow pointer fly-overs.
  return screen.findByRole('dialog');
}

beforeEach(() => {
  localStorage.removeItem('printerCardSize');
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json([printer])),
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
    http.get('/api/v1/settings/ui-preferences', () =>
      HttpResponse.json({
        ams_humidity_good: 40,
        ams_humidity_fair: 60,
        ams_temp_good: 30,
        ams_temp_fair: 35,
        require_plate_clear: true,
      }),
    ),
    http.get('/api/v1/spoolman/settings', () =>
      HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' }),
    ),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
  );
});

afterEach(() => {
  server.resetHandlers();
});

describe('PrintersPage — "Re-check slot" verdicts', () => {
  it.each([
    [
      'unchanged',
      {},
      `Slot ${SLOT_LABEL} unchanged. No roll movement recorded since the last check.`,
    ],
    [
      'minted',
      { verdict: 'minted', spool_id: 99, label_weight_g: 1000, undo_available: false },
      `Slot ${SLOT_LABEL}: new roll recorded. ${(1000).toLocaleString()} g assumed.`,
    ],
    [
      'identified',
      { verdict: 'identified', brand: 'Bambu Lab', material: 'PETG' },
      `Slot ${SLOT_LABEL}: Bambu Lab PETG recorded from its tag.`,
    ],
    [
      'queued',
      { verdict: 'queued' },
      `Slot ${SLOT_LABEL} re-check queued. The farm reads the tag at the next idle window and records the roll then.`,
    ],
    ['empty', { verdict: 'empty' }, `Slot ${SLOT_LABEL} is empty. Load a roll, then re-check.`],
    [
      'restored',
      { verdict: 'restored', spool_id: 42 },
      `Slot ${SLOT_LABEL}: previous roll restored.`,
    ],
  ])('verdict "%s" is announced as its own sentence', async (_verdict, overrides, sentence) => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(recheckResponse(overrides)),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    expect(await screen.findByText(sentence)).toBeInTheDocument();
  });

  it('falls back to a tag-read sentence when the tag names neither brand nor material', async () => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(recheckResponse({ verdict: 'identified' })),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    expect(await screen.findByText(`Slot ${SLOT_LABEL}: tag read.`)).toBeInTheDocument();
  });

  it('names the deferral when the identity is on record but no read went out', async () => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(
          recheckResponse({
            verdict: 'identified',
            brand: 'Bambu Lab',
            material: 'PETG',
            read_issued: false,
          }),
        ),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    // "recorded from its tag" would claim a read the AMS refused to run.
    expect(
      await screen.findByText(`Slot ${SLOT_LABEL}: Bambu Lab PETG on record. Tag re-read deferred.`),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(`Slot ${SLOT_LABEL}: Bambu Lab PETG recorded from its tag.`),
    ).not.toBeInTheDocument();
  });

  it('offers "Restore previous roll" on the acknowledgement of a mint', async () => {
    server.use(
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () =>
        HttpResponse.json(
          recheckResponse({
            verdict: 'minted',
            spool_id: 99,
            label_weight_g: 1000,
            undo_available: true,
          }),
        ),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(within(card).getByRole('button', { name: 'Re-check slot' }));

    const toast = await screen.findByText(
      `Slot ${SLOT_LABEL}: new roll recorded. ${(1000).toLocaleString()} g assumed.`,
    );
    const toastRow = toast.closest('div') as HTMLElement;
    expect(
      within(toastRow).getByRole('button', { name: 'Restore previous roll' }),
    ).toBeInTheDocument();
  });
});

describe('PrintersPage — standing undo on the slot card', () => {
  it('renders "Restore previous roll" while the offer stands, and drops it once undone', async () => {
    let undone = false;
    server.use(
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_undo_available: !undone })]),
      ),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck/undo`, () => {
        undone = true;
        return HttpResponse.json(recheckResponse({ verdict: 'restored', spool_id: 42 }));
      }),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    const undo = await within(card).findByRole('button', { name: 'Restore previous roll' });
    // An offer, not an interruption: it never takes focus for itself.
    expect(undo).not.toHaveFocus();

    await userEvent.click(undo);

    expect(
      await screen.findByText(`Slot ${SLOT_LABEL}: previous roll restored.`),
    ).toBeInTheDocument();
    expect(undone).toBe(true);
    await waitFor(() => {
      expect(
        within(card).queryByRole('button', { name: 'Restore previous roll' }),
      ).not.toBeInTheDocument();
    });
    // …and the card is still the open card, not a torn-down one: the verb
    // disappeared because the offer lapsed, not because the surface vanished.
    expect(within(card).getByRole('button', { name: 'Load' })).toBeInTheDocument();
  });

  /** Every undo refusal is a structured `{code, message}` 409. The UI must key
   *  its copy off `code`: `message` is the backend's English fallback for curl
   *  and scripts, so matching on it would have shown raw English in the ten
   *  non-en locales and broken outright on any rewording. The `message` served
   *  below is deliberately NOT the localized sentence, so a test that passes
   *  proves the code drove the lookup. */
  it('names the reason when the standing offer is already gone (409)', async () => {
    server.use(
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_undo_available: true })]),
      ),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck/undo`, () =>
        HttpResponse.json(
          {
            detail: {
              code: 'no_offer',
              message:
                'No restore offer stands for this slot: the row bound here was not created by a re-check, or the slot has been re-decided since.',
            },
          },
          { status: 409 },
        ),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(
      await within(card).findByRole('button', { name: 'Restore previous roll' }),
    );

    expect(
      await screen.findByText(`Slot ${SLOT_LABEL}: no re-check to undo.`),
    ).toBeInTheDocument();
  });

  it('names the reason when the previous roll is bound to another slot (409)', async () => {
    server.use(
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_undo_available: true })]),
      ),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck/undo`, () =>
        HttpResponse.json(
          {
            detail: {
              code: 'predecessor_bound_elsewhere',
              message:
                'The previous roll is bound to another slot now; restoring it here would put one physical roll in two trays.',
            },
          },
          { status: 409 },
        ),
      ),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await userEvent.click(
      await within(card).findByRole('button', { name: 'Restore previous roll' }),
    );

    expect(
      await screen.findByText(
        `Slot ${SLOT_LABEL}: previous roll is seated in another tray. Nothing restored.`,
      ),
    ).toBeInTheDocument();
  });
});

/**
 * The queued path. "Re-check slot" was disabled while the printer was RUNNING
 * and while any filament was engaged, so the `queued` verdict — the whole
 * reason the durable intent table exists — could not be reached from the UI at
 * all. It is reachable now, and because the conclusion lands silently minutes
 * later, the outstanding check is carried as a STATE on the slot rather than
 * announced as an event.
 */
describe('PrintersPage — a re-check queued mid-print', () => {
  it('queues a re-check mid-print and renders the pending line', async () => {
    let pending = false;
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printingStatus)),
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_pending: pending })]),
      ),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck`, () => {
        pending = true;
        return HttpResponse.json(recheckResponse({ verdict: 'queued' }));
      }),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    const recheck = within(card).getByRole('button', { name: 'Re-check slot' });
    // Neither the running print nor the engaged filament refuses the click —
    // they only move the answer later, which the title states up front.
    expect(recheck).toBeEnabled();
    expect(recheck).toHaveAttribute('title', 'Queued until the print finishes.');

    await userEvent.click(recheck);

    expect(
      await screen.findByText(
        `Slot ${SLOT_LABEL} re-check queued. The farm reads the tag at the next idle window and records the roll then.`,
      ),
    ).toBeInTheDocument();

    // The acknowledgement fades; the state does not. The verb reports it…
    const pendingVerb = await within(card).findByRole('button', { name: PENDING });
    expect(pendingVerb).toBeDisabled();
    // …and the card states the same sentence on a line of its own, outside the
    // verb, so it is readable without hunting through the action list.
    const mentions = within(card).getAllByText(PENDING);
    expect(mentions.some((el) => el.closest('button') === null)).toBe(true);
  });

  it('the pending line clears when the intent concludes', async () => {
    let concluded = false;
    server.use(
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printingStatus)),
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([
          assignmentRow(
            concluded
              ? { recheck_pending: false, recheck_undo_available: true }
              : { recheck_pending: true, recheck_undo_available: false },
          ),
        ]),
      ),
      // The intent concludes server-side at the next answerable read, with no
      // announcement of its own — an assignment refetch is what carries the
      // conclusion to the card. Any of them will do; a re-check on the OTHER
      // slot is the one this harness can trigger on demand.
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/0/recheck`, () => {
        concluded = true;
        return HttpResponse.json(
          recheckResponse({ verdict: 'minted', tray_id: 0, spool_id: 99, label_weight_g: 1000 }),
        );
      }),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    expect(await within(card).findByRole('button', { name: PENDING })).toBeDisabled();
    await closeSlotCard(TRAY_ID);

    const otherCard = await openSlotCard(0);
    await userEvent.click(within(otherCard).getByRole('button', { name: 'Re-check slot' }));
    expect(await screen.findByText(/Slot 1: new roll recorded/)).toBeInTheDocument();
    await closeSlotCard(0);

    const reopened = await openSlotCard(TRAY_ID);
    await waitFor(() => expect(within(reopened).queryAllByText(PENDING)).toHaveLength(0));
    // The verb is offered again, and the mint the conclusion recorded carries
    // its own standing undo.
    expect(within(reopened).getByRole('button', { name: 'Re-check slot' })).toBeInTheDocument();
    expect(
      await within(reopened).findByRole('button', { name: 'Restore previous roll' }),
    ).toBeInTheDocument();
  });

  it('re-check stays disabled while the AMS is drying', async () => {
    server.use(http.get('/api/v1/printers/:id/status', () => HttpResponse.json(dryingStatus)));
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    const recheck = within(card).getByRole('button', { name: 'Re-check slot' });
    expect(recheck).toBeDisabled();
    expect(recheck).toHaveAttribute('title', 'Not available while the AMS is drying');
  });
});

describe('PrintersPage — the standing undo lapses with the row it comes from', () => {
  // The verb is rendered purely from the backend's derived
  // `recheck_undo_available`, so an offer withdrawn server-side has to vanish
  // from the card at the next refetch — with no undo of the operator's own.
  // Before the keyboard work this was only ever observed through a click; it is
  // asserted here because the card is now a durable, focusable surface that can
  // sit open while the data underneath it changes.
  it('drops "Restore previous roll" when the refetched assignment no longer carries the offer', async () => {
    let offerStands = true;
    let undoCalls = 0;
    server.use(
      http.get('/api/v1/inventory/assignments', () =>
        HttpResponse.json([assignmentRow({ recheck_undo_available: offerStands })]),
      ),
      // A re-check on the OTHER slot: its mint invalidates the assignment
      // queries, which is what pulls the withdrawn offer back to this card.
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/0/recheck`, () => {
        offerStands = false;
        return HttpResponse.json(
          recheckResponse({ verdict: 'minted', tray_id: 0, spool_id: 99, label_weight_g: 1000 }),
        );
      }),
      http.post(`/api/v1/printers/1/ams/${AMS_ID}/slot/${TRAY_ID}/recheck/undo`, () => {
        undoCalls += 1;
        return HttpResponse.json(recheckResponse({ verdict: 'restored', spool_id: 42 }));
      }),
    );
    render(<PrintersPage />);

    const card = await openSlotCard(TRAY_ID);
    await within(card).findByRole('button', { name: 'Restore previous roll' });
    await closeSlotCard(TRAY_ID);

    const otherCard = await openSlotCard(0);
    await userEvent.click(within(otherCard).getByRole('button', { name: 'Re-check slot' }));
    expect(await screen.findByText(/Slot 1: new roll recorded/)).toBeInTheDocument();
    await closeSlotCard(0);

    const reopened = await openSlotCard(TRAY_ID);
    // The card is real (its other verbs are there) — the offer is what lapsed.
    expect(within(reopened).getByRole('button', { name: 'Load' })).toBeInTheDocument();
    await waitFor(() =>
      expect(
        within(reopened).queryByRole('button', { name: 'Restore previous roll' }),
      ).not.toBeInTheDocument(),
    );
    // …and nothing the operator did withdrew it.
    expect(undoCalls).toBe(0);
  });
});
