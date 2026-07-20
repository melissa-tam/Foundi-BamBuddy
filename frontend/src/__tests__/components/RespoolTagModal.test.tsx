import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { RespoolTagModal } from '../../components/RespoolTagModal';
import { api } from '../../api/client';
import type { RespoolPromptMessage } from '../../api/client';

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
    respoolTag: vi.fn(),
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

const CONFIRM = /Re-spooled — fresh spool/i;

function makeContext(overrides: Partial<RespoolPromptMessage> = {}): RespoolPromptMessage {
  return {
    printer_id: 7,
    ams_id: 0,
    tray_id: 2,
    tag_uid: 'DEADBEEF',
    tray_uuid: null,
    tray_type: 'PETG',
    tray_color: 'FF8800FF',
    tray_sub_brands: 'PETG HF',
    tray_count: 4,
    donor_spool_id: 12,
    donor_remaining_g: 18,
    brand_prefill: null,
    label_weight_prefill: 1000,
    ...overrides,
  };
}

describe('RespoolTagModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue(SPOOLS);
    try {
      window.localStorage.clear();
    } catch {
      /* jsdom localStorage — ignore */
    }
  });

  it('renders nothing without a context', () => {
    render(<RespoolTagModal context={null} onClose={vi.fn()} />);
    expect(screen.queryByRole('button', { name: CONFIRM })).not.toBeInTheDocument();
    expect(screen.queryByText(/ONE tag per donor roll/i)).not.toBeInTheDocument();
  });

  it('renders material headline, warning, and a de-jargoned donor line (no record id in visible text)', () => {
    render(<RespoolTagModal context={makeContext()} onClose={vi.fn()} />);
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
    render(<RespoolTagModal context={makeContext({ trigger: 'near_empty' })} onClose={vi.fn()} />);
    expect(screen.getByText(/Replacing this roll\?/i)).toBeInTheDocument();
    expect(screen.getByText(/almost empty/i)).toBeInTheDocument();
    expect(screen.queryByText(/reused Bambu tag was detected/i)).not.toBeInTheDocument();
    // The re-spool form itself is unchanged — same actions, same fields.
    expect(screen.getByRole('button', { name: CONFIRM })).toBeInTheDocument();
    expect(screen.getByLabelText('Brand')).toBeInTheDocument();
  });

  it('keeps the reused-tag framing for a remain_jump prompt and names the evidence', () => {
    render(<RespoolTagModal context={makeContext({ trigger: 'remain_jump' })} onClose={vi.fn()} />);
    expect(screen.getByText(/reused Bambu tag was detected/i)).toBeInTheDocument();
    expect(screen.getByText(/far more filament than this record holds/i)).toBeInTheDocument();
  });

  it('keeps the reused-tag framing for a spent prompt and names the evidence', () => {
    render(<RespoolTagModal context={makeContext({ trigger: 'spent' })} onClose={vi.fn()} />);
    expect(screen.getByText(/reused Bambu tag was detected/i)).toBeInTheDocument();
    expect(screen.getByText(/reported running out/i)).toBeInTheDocument();
  });

  it('falls back to the reused-tag framing when no trigger is supplied (manual tray-menu path)', () => {
    render(<RespoolTagModal context={makeContext()} onClose={vi.fn()} />);
    expect(screen.getByText(/reused Bambu tag was detected/i)).toBeInTheDocument();
    expect(screen.queryByText(/Replacing this roll\?/i)).not.toBeInTheDocument();
  });

  it('keeps the raw tag UID inside the collapsed Details disclosure, not the headline', () => {
    render(<RespoolTagModal context={makeContext()} onClose={vi.fn()} />);
    const uid = screen.getByText('DEADBEEF');
    // The UID lives inside a <details> disclosure (in the DOM but not the headline).
    expect(uid.closest('details')).not.toBeNull();
    const details = uid.closest('details') as HTMLDetailsElement;
    expect(within(details).getByText(/Details/i)).toBeInTheDocument();
  });

  it('prefills the brand from brand_prefill', () => {
    render(<RespoolTagModal context={makeContext({ brand_prefill: 'Sunlu' })} onClose={vi.fn()} />);
    expect(screen.getByLabelText('Brand')).toHaveValue('Sunlu');
  });

  it('offers the distinct non-archived brands as datalist options', async () => {
    // ConfirmModal portals its body, so query the whole document, not `container`.
    render(<RespoolTagModal context={makeContext()} onClose={vi.fn()} />);
    await waitFor(() => {
      const options = Array.from(
        document.querySelectorAll('datalist#respool-brand-options option'),
      ).map((o) => (o as HTMLOptionElement).value);
      // Overture + Sunlu, sorted; archived "Archived Co" excluded.
      expect(options).toEqual(['Overture', 'Sunlu']);
    });
  });

  it('prefills cost/kg from the most recent same-brand+material spool', async () => {
    render(<RespoolTagModal context={makeContext({ brand_prefill: 'Overture' })} onClose={vi.fn()} />);
    // Overture has a PETG spool (22) and a PLA spool (25); tag material is PETG → 22.
    await waitFor(() => expect(screen.getByLabelText('Cost / kg')).toHaveValue(22));
  });

  it('recomputes cost on brand change until the operator edits it manually', async () => {
    const user = userEvent.setup();
    render(<RespoolTagModal context={makeContext({ brand_prefill: 'Overture' })} onClose={vi.fn()} />);
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
    render(<RespoolTagModal context={makeContext({ brand_prefill: null })} onClose={vi.fn()} />);
    const confirm = screen.getByRole('button', { name: CONFIRM });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText('Brand'), 'Overture');
    expect(confirm).toBeEnabled();
  });

  it('submits the expected re-spool payload shape', async () => {
    const user = userEvent.setup();
    (api.respoolTag as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 31, brand: 'Overture' });
    const onClose = vi.fn();
    render(<RespoolTagModal context={makeContext({ brand_prefill: 'Overture' })} onClose={onClose} />);

    // Wait for the async cost prefill to settle so the payload is deterministic.
    await waitFor(() => expect(screen.getByLabelText('Cost / kg')).toHaveValue(22));
    await user.click(screen.getByRole('button', { name: CONFIRM }));

    await waitFor(() => {
      expect(api.respoolTag).toHaveBeenCalledWith({
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

  it('surfaces the backend error message as an error toast', async () => {
    const user = userEvent.setup();
    (api.respoolTag as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Spoolman mode enabled'));
    render(<RespoolTagModal context={makeContext({ brand_prefill: 'Overture' })} onClose={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: CONFIRM }));

    await waitFor(() => {
      expect(mockShowToast).toHaveBeenCalledWith('Spoolman mode enabled', 'error');
    });
  });
});
