import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { RespoolTagModal } from '../../components/RespoolTagModal';
import { api } from '../../api/client';
import type { RespoolPromptMessage } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    respoolTag: vi.fn(),
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

  it('renders the payload — material, tag identity, donor line, and warning', () => {
    render(<RespoolTagModal context={makeContext()} onClose={vi.fn()} />);
    expect(screen.getByText('PETG HF')).toBeInTheDocument();
    expect(screen.getByText('DEADBEEF')).toBeInTheDocument();
    // Donor line carries the record id and remaining grams.
    const donor = screen.getByText(/#12/);
    expect(donor).toHaveTextContent('18');
    // Persistent one-tag-per-roll warning.
    expect(screen.getByText(/ONE tag per donor roll/i)).toBeInTheDocument();
  });

  it('prefills the brand from brand_prefill', () => {
    render(<RespoolTagModal context={makeContext({ brand_prefill: 'Sunlu' })} onClose={vi.fn()} />);
    expect(screen.getByLabelText('Brand')).toHaveValue('Sunlu');
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

    await user.click(screen.getByRole('button', { name: CONFIRM }));

    await waitFor(() => {
      expect(api.respoolTag).toHaveBeenCalledWith({
        printer_id: 7,
        ams_id: 0,
        tray_id: 2,
        brand: 'Overture',
        label_weight: 1000,
        cost_per_kg: null,
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
