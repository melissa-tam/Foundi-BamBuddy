/**
 * Tests for `NewRollModal` — the ONE form behind the ONE "New roll…" slot verb
 * (B4 slot-verb consolidation).
 *
 * It replaced two forms ("Re-spool tag…" for tagged rows, "New roll…" for tagless
 * ones) that asked the operator the same question and differed only in the
 * bookkeeping behind it. The suite therefore pins BOTH lanes on ONE component and
 * ONE request (`POST /inventory/spools/{id}/new-roll`):
 *  - tag-ness drives the copy, the brand requirement and the one-tag warning,
 *  - `origin` drives manual-assertion vs prompt-answer framing,
 *  - a backend refusal stays on screen inside the dialog either way.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { NewRollModal } from '../../components/NewRollModal';
import { manualNewRollContext } from '../../utils/newRollContext';
import type { NewRollContext } from '../../utils/newRollContext';
import { api } from '../../api/client';
import type { InventorySpool } from '../../api/client';

// Inventory rows feeding the brand datalist + cost/kg prefill. Only the fields
// the modal reads are populated. Hoisted so the (hoisted) vi.mock factory below
// can reference it without a TDZ error.
const { SPOOLS } = vi.hoisted(() => ({
  SPOOLS: [
    { id: 1, brand: 'Overture', material: 'PETG', cost_per_kg: 22, archived_at: null, created_at: '2026-07-01T00:00:00Z' },
    { id: 2, brand: 'Sunlu', material: 'PLA', cost_per_kg: 18, archived_at: null, created_at: '2026-07-02T00:00:00Z' },
    { id: 3, brand: 'Overture', material: 'PLA', cost_per_kg: 25, archived_at: null, created_at: '2026-07-03T00:00:00Z' },
    { id: 4, brand: 'Archived Co', material: 'PETG', cost_per_kg: 99, archived_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z' },
  ],
}));

vi.mock('../../api/client', () => ({
  api: {
    newRoll: vi.fn(),
    getSpools: vi.fn().mockResolvedValue(SPOOLS),
    dismissRespoolPrompt: vi.fn(),
    // Provider dependencies (ThemeProvider / AuthProvider mount).
    getSettings: vi.fn().mockResolvedValue({}),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
  },
}));

const mockShowToast = vi.fn();
vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return {
    ...actual,
    useToast: () => ({ showToast: mockShowToast }),
  };
});

/** The tagged (reused Bambu tag) lane, opened from a prompt. */
function tagged(overrides: Partial<NewRollContext> = {}): NewRollContext {
  return {
    printer_id: 7,
    ams_id: 0,
    tray_id: 2,
    spool_id: 12,
    tagged: true,
    origin: 'prompt',
    tray_count: 4,
    material: 'PETG HF',
    rgba: 'FF8800FF',
    tag_identity: 'DEADBEEF',
    remaining_g: 18,
    brand_prefill: null,
    label_weight_prefill: 1000,
    ...overrides,
  };
}

/** The tagless lane. */
function tagless(overrides: Partial<NewRollContext> = {}): NewRollContext {
  return {
    printer_id: 7,
    ams_id: 0,
    tray_id: 2,
    spool_id: 140,
    tagged: false,
    origin: 'prompt',
    material: 'PETG',
    rgba: 'FF8800FF',
    remaining_g: 180,
    ...overrides,
  };
}

const TAGGED_CONFIRM = /Re-spooled — fresh spool/i;
const TAGLESS_MANUAL_CONFIRM = /Mark as new roll/i;

describe('NewRollModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue(SPOOLS);
    (api.newRoll as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 201, brand: 'Overture' });
    try {
      window.localStorage.clear();
    } catch {
      /* jsdom localStorage — ignore */
    }
  });

  it('renders nothing without a context', () => {
    render(<NewRollModal context={null} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: TAGGED_CONFIRM })).not.toBeInTheDocument();
    expect(screen.queryByText(/ONE tag per donor roll/i)).not.toBeInTheDocument();
  });

  // ── tagged lane (reused Bambu tag) ────────────────────────────────────────
  describe('tagged row', () => {
    it('renders material headline, warning, and a de-jargoned donor line (no record id in visible text)', () => {
      render(<NewRollModal context={tagged()} onClose={vi.fn()} />);
      expect(screen.getByText('PETG HF')).toBeInTheDocument();
      // Donor line shows grams remaining but NOT the internal record id.
      const donor = screen.getByText(/Replaces the tracked spool/i);
      expect(donor).toHaveTextContent('18');
      expect(screen.queryByText(/#12/)).not.toBeInTheDocument();
      // The record id survives only as a title (hover) attribute.
      expect(donor).toHaveAttribute('title', expect.stringContaining('12'));
      // Persistent one-tag-per-roll warning.
      expect(screen.getByText(/ONE tag per donor roll/i)).toBeInTheDocument();
    });

    // -- trigger-driven framing (2026-07-20: the copy claimed a reused tag had been
    //    detected no matter WHY the prompt fired, which is how two false "reused
    //    RFID spool" popups reached an operator whose farm reuses no tags) --------

    it('frames a near_empty prompt as a roll replacement, with no reused-tag language', () => {
      render(<NewRollModal context={tagged({ trigger: 'near_empty' })} onClose={vi.fn()} />);
      expect(screen.getByText(/Replacing this roll\?/i)).toBeInTheDocument();
      expect(screen.getByText(/almost empty/i)).toBeInTheDocument();
      expect(screen.queryByText(/reused Bambu tag was detected/i)).not.toBeInTheDocument();
      // The form itself is unchanged — same actions, same fields.
      expect(screen.getByRole('button', { name: TAGGED_CONFIRM })).toBeInTheDocument();
      expect(screen.getByLabelText('Brand')).toBeInTheDocument();
    });

    it('keeps the reused-tag framing for a remain_jump prompt and names the evidence', () => {
      render(<NewRollModal context={tagged({ trigger: 'remain_jump' })} onClose={vi.fn()} />);
      expect(screen.getByText(/reused Bambu tag was detected/i)).toBeInTheDocument();
      expect(screen.getByText(/far more filament than this record holds/i)).toBeInTheDocument();
    });

    it('keeps the reused-tag framing for a spent prompt and names the evidence', () => {
      render(<NewRollModal context={tagged({ trigger: 'spent' })} onClose={vi.fn()} />);
      expect(screen.getByText(/reused Bambu tag was detected/i)).toBeInTheDocument();
      expect(screen.getByText(/reported running out/i)).toBeInTheDocument();
    });

    it('states the operator ASSERTION on a manual open rather than claiming a detection', () => {
      // The operator's own click is not evidence of anything the farm observed
      // (doctrine rule 10), so the copy must not say a reused tag "was detected".
      render(<NewRollModal context={tagged({ origin: 'manual', used_g: 982 })} onClose={vi.fn()} />);
      expect(screen.getByText(/Move this tag onto a new roll\?/i)).toBeInTheDocument();
      const message = screen.getByText(/Retire the record at/i);
      expect(message).toHaveTextContent('982 g used');
      expect(message).toHaveTextContent('Slot 3');
      expect(screen.queryByText(/reused Bambu tag was detected/i)).not.toBeInTheDocument();
    });

    it('keeps the raw tag UID inside the collapsed Details disclosure, not the headline', () => {
      render(<NewRollModal context={tagged()} onClose={vi.fn()} />);
      const uid = screen.getByText('DEADBEEF');
      // The UID lives inside a <details> disclosure (in the DOM but not the headline).
      expect(uid.closest('details')).not.toBeNull();
      const details = uid.closest('details') as HTMLDetailsElement;
      expect(within(details).getByText(/Details/i)).toBeInTheDocument();
    });

    it('prefills the brand from brand_prefill', () => {
      render(<NewRollModal context={tagged({ brand_prefill: 'Sunlu' })} onClose={vi.fn()} />);
      expect(screen.getByLabelText('Brand')).toHaveValue('Sunlu');
    });

    it('offers the distinct non-archived brands as datalist options', async () => {
      // ConfirmModal portals its body, so query the whole document, not `container`.
      render(<NewRollModal context={tagged()} onClose={vi.fn()} />);
      await waitFor(() => {
        const options = Array.from(
          document.querySelectorAll('datalist#new-roll-brand-options option'),
        ).map((o) => (o as HTMLOptionElement).value);
        // Overture + Sunlu, sorted; archived "Archived Co" excluded.
        expect(options).toEqual(['Overture', 'Sunlu']);
      });
    });

    it('prefills cost/kg from the most recent same-brand+material spool', async () => {
      render(<NewRollModal context={tagged({ brand_prefill: 'Overture', material: 'PETG' })} onClose={vi.fn()} />);
      // Overture has a PETG spool (22) and a PLA spool (25); slot material is PETG → 22.
      await waitFor(() => expect(screen.getByLabelText('Cost / kg')).toHaveValue(22));
    });

    it('recomputes cost on brand change until the operator edits it manually', async () => {
      const user = userEvent.setup();
      render(<NewRollModal context={tagged({ brand_prefill: 'Overture', material: 'PETG' })} onClose={vi.fn()} />);
      const cost = screen.getByLabelText('Cost / kg');
      await waitFor(() => expect(cost).toHaveValue(22));

      // Switch brand → cost follows (Sunlu has only a PLA spool at 18).
      const brand = screen.getByLabelText('Brand');
      await user.clear(brand);
      await user.type(brand, 'Sunlu');
      await waitFor(() => expect(cost).toHaveValue(18));

      // Edit cost manually → it is now "touched" and no longer auto-recomputes.
      await user.clear(cost);
      await user.type(cost, '5');
      await user.clear(brand);
      await user.type(brand, 'Overture');
      // Give any (suppressed) recompute a chance to run, then assert it held.
      await new Promise((r) => setTimeout(r, 0));
      expect(cost).toHaveValue(5);
    });

    it('disables confirm until a brand is entered', async () => {
      const user = userEvent.setup();
      render(<NewRollModal context={tagged({ brand_prefill: null })} onClose={vi.fn()} />);
      const confirm = screen.getByRole('button', { name: TAGGED_CONFIRM });
      expect(confirm).toBeDisabled();
      await user.type(screen.getByLabelText('Brand'), 'Overture');
      expect(confirm).toBeEnabled();
    });

    it('posts the merged new-roll request keyed by the BOUND spool id', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      render(<NewRollModal context={tagged({ brand_prefill: 'Overture', material: 'PETG' })} onClose={onClose} />);

      // Wait for the async cost prefill to settle so the payload is deterministic.
      await waitFor(() => expect(screen.getByLabelText('Cost / kg')).toHaveValue(22));
      await user.click(screen.getByRole('button', { name: TAGGED_CONFIRM }));

      await waitFor(() => {
        expect(api.newRoll).toHaveBeenCalledWith(12, {
          printer_id: 7,
          ams_id: 0,
          tray_id: 2,
          brand: 'Overture',
          label_weight: 1000,
          // Cost prefills from the same-brand PETG spool (22).
          cost_per_kg: 22,
          note: null,
        });
      });
      await waitFor(() => expect(onClose).toHaveBeenCalled());
    });

    it('keeps a backend refusal on screen inside the dialog instead of toasting it away', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      (api.newRoll as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Spoolman mode enabled'));
      render(<NewRollModal context={tagged({ brand_prefill: 'Overture' })} onClose={vi.fn()} />);

      await user.click(screen.getByRole('button', { name: TAGGED_CONFIRM }));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('Spoolman mode enabled');
      expect(onClose).not.toHaveBeenCalled();
      expect(mockShowToast).not.toHaveBeenCalledWith(expect.anything(), 'error');
    });
  });

  // ── tagless lane ─────────────────────────────────────────────────────────
  describe('tagless row', () => {
    it('states that the current record is retired, naming the grams already used', () => {
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={vi.fn()} />);

      expect(screen.getByText(/Mark as a new roll\?/i)).toBeInTheDocument();
      const message = screen.getByText(/Retire the record at/i);
      expect(message).toHaveTextContent('820 g used');
      // Slot location is spelled out so the operator can check they picked the
      // right tray before archiving a ledger row.
      expect(message).toHaveTextContent('Slot 3');
      // The prompt-path framing (consumed past its label weight) must NOT leak
      // into a manual open — nothing was measured here.
      expect(screen.queryByText(/used past most of its label weight/i)).not.toBeInTheDocument();
    });

    it('carries no reused-tag warning or tag disclosure', () => {
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={vi.fn()} />);
      expect(screen.queryByText(/ONE tag per donor roll/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/Details/i)).not.toBeInTheDocument();
    });

    it('labels the confirm button for the manual action', () => {
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={vi.fn()} />);
      expect(screen.getByRole('button', { name: TAGLESS_MANUAL_CONFIRM })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^Fresh roll$/i })).not.toBeInTheDocument();
    });

    it('treats a missing used_g as zero rather than rendering a blank', () => {
      render(<NewRollModal context={tagless({ origin: 'manual' })} onClose={vi.fn()} />);
      expect(screen.getByText(/Retire the record at/i)).toHaveTextContent('0 g used');
    });

    it('needs no brand — the tagless mint falls back to the configured default', () => {
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={vi.fn()} />);
      expect(screen.getByRole('button', { name: TAGLESS_MANUAL_CONFIRM })).toBeEnabled();
    });

    it('posts the merged new-roll request for the BOUND spool id and closes on success', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={onClose} />);

      await user.click(screen.getByRole('button', { name: TAGLESS_MANUAL_CONFIRM }));

      await waitFor(() => expect(api.newRoll).toHaveBeenCalledTimes(1));
      expect(api.newRoll).toHaveBeenCalledWith(140, expect.objectContaining({
        printer_id: 7,
        ams_id: 0,
        tray_id: 2,
      }));
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(mockShowToast).toHaveBeenCalledWith(expect.stringMatching(/fresh roll/i), 'success');
    });

    it('carries the optional brand through to the new row', async () => {
      const user = userEvent.setup();
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={vi.fn()} />);

      await user.type(screen.getByLabelText(/Brand/i), 'Overture');
      await user.click(screen.getByRole('button', { name: TAGLESS_MANUAL_CONFIRM }));

      await waitFor(() => expect(api.newRoll).toHaveBeenCalledTimes(1));
      expect(api.newRoll).toHaveBeenCalledWith(140, expect.objectContaining({ brand: 'Overture' }));
    });

    it('keeps a backend refusal on screen inside the dialog instead of toasting it away', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      (api.newRoll as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error('Spool #140 is not assigned to printer 7 AMS 0 slot 3.'),
      );
      render(<NewRollModal context={tagless({ origin: 'manual', used_g: 820 })} onClose={onClose} />);

      await user.click(screen.getByRole('button', { name: TAGLESS_MANUAL_CONFIRM }));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('not assigned');
      // Dialog stays open so the operator can read the reason and retry.
      expect(onClose).not.toHaveBeenCalled();
      expect(mockShowToast).not.toHaveBeenCalledWith(expect.anything(), 'error');
    });

    it('keeps the consumption framing and the "Fresh roll" confirm on a prompt open', () => {
      render(<NewRollModal context={tagless()} onClose={vi.fn()} />);
      expect(screen.getByText(/Fresh roll on this slot\?/i)).toBeInTheDocument();
      expect(screen.getByText(/used past most of its label weight/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Fresh roll/i })).toBeInTheDocument();
      expect(screen.queryByText(/Retire the record at/i)).not.toBeInTheDocument();
    });
  });

  // ── the shared manual-context builder ────────────────────────────────────
  describe('manualNewRollContext', () => {
    const row = (over: Partial<InventorySpool> = {}) =>
      ({
        id: 55,
        material: 'PETG',
        rgba: 'FF8800FF',
        label_weight: 1000,
        weight_used: 300,
        tag_uid: null,
        tray_uuid: null,
        ...over,
      }) as InventorySpool;

    it('reads tag-ness from EITHER identity column, so a tray_uuid-only row is tagged', () => {
      expect(manualNewRollContext(7, 0, 2, row()).tagged).toBe(false);
      expect(manualNewRollContext(7, 0, 2, row({ tag_uid: 'AA' })).tagged).toBe(true);
      expect(manualNewRollContext(7, 0, 2, row({ tray_uuid: 'BB' })).tagged).toBe(true);
    });

    it('carries the slot, the bound row id and both gram figures', () => {
      const ctx = manualNewRollContext(7, 1, 3, row(), 4);
      expect(ctx).toMatchObject({
        printer_id: 7,
        ams_id: 1,
        tray_id: 3,
        spool_id: 55,
        origin: 'manual',
        tray_count: 4,
        used_g: 300,
        remaining_g: 700,
      });
    });

    it('reads a spent roll as 0 g remaining, matching every other surface', () => {
      const ctx = manualNewRollContext(7, 0, 0, row({ spent_at: '2026-08-01T00:00:00Z' }));
      expect(ctx.remaining_g).toBe(0);
    });
  });
});
