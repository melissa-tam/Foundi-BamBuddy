/**
 * Tests for TaglessFreshModal, with the emphasis on the W5a operator-initiated
 * "New roll…" verb (`manual`): it is the ONLY swap signal a tagless slot's
 * ledger ever gets, so the copy must state plainly what is retired, the request
 * must carry the BOUND spool id, and a backend refusal must stay on screen.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { TaglessFreshModal } from '../../components/TaglessFreshModal';
import { api } from '../../api/client';
import type { TaglessFreshPromptMessage } from '../../api/client';

const { SPOOLS } = vi.hoisted(() => ({
  SPOOLS: [
    { id: 1, brand: 'Overture', material: 'PETG', cost_per_kg: 22, archived_at: null, created_at: '2026-07-01T00:00:00Z' },
  ],
}));

vi.mock('../../api/client', () => ({
  api: {
    taglessFresh: vi.fn(),
    getSpools: vi.fn().mockResolvedValue(SPOOLS),
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

function makeContext(overrides: Partial<TaglessFreshPromptMessage> = {}): TaglessFreshPromptMessage {
  return {
    printer_id: 7,
    ams_id: 0,
    tray_id: 2,
    spool_id: 140,
    remaining_g: 180,
    material: 'PETG',
    rgba: 'FF8800FF',
    ...overrides,
  };
}

describe('TaglessFreshModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getSpools as ReturnType<typeof vi.fn>).mockResolvedValue(SPOOLS);
    (api.taglessFresh as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 201 });
  });

  it('renders nothing without a context', () => {
    render(<TaglessFreshModal context={null} onClose={vi.fn()} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  describe('operator-initiated "New roll…" (manual)', () => {
    it('states that the current record is retired, naming the grams already used', () => {
      render(<TaglessFreshModal context={makeContext()} usedGrams={820} manual onClose={vi.fn()} />);

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

    it('labels the confirm button for the manual action', () => {
      render(<TaglessFreshModal context={makeContext()} usedGrams={820} manual onClose={vi.fn()} />);
      expect(screen.getByRole('button', { name: /Mark as new roll/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^Fresh roll$/i })).not.toBeInTheDocument();
    });

    it('treats a missing usedGrams as zero rather than rendering a blank', () => {
      render(<TaglessFreshModal context={makeContext()} manual onClose={vi.fn()} />);
      expect(screen.getByText(/Retire the record at/i)).toHaveTextContent('0 g used');
    });

    it('posts answer "fresh" for the BOUND spool id and closes on success', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      render(<TaglessFreshModal context={makeContext()} usedGrams={820} manual onClose={onClose} />);

      await user.click(screen.getByRole('button', { name: /Mark as new roll/i }));

      await waitFor(() => expect(api.taglessFresh).toHaveBeenCalledTimes(1));
      expect(api.taglessFresh).toHaveBeenCalledWith(140, expect.objectContaining({
        printer_id: 7,
        ams_id: 0,
        tray_id: 2,
        answer: 'fresh',
      }));
      await waitFor(() => expect(onClose).toHaveBeenCalled());
      expect(mockShowToast).toHaveBeenCalledWith(expect.stringMatching(/fresh roll/i), 'success');
    });

    it('carries the optional brand through to the new row', async () => {
      const user = userEvent.setup();
      render(<TaglessFreshModal context={makeContext()} usedGrams={820} manual onClose={vi.fn()} />);

      await user.type(screen.getByLabelText(/Brand/i), 'Overture');
      await user.click(screen.getByRole('button', { name: /Mark as new roll/i }));

      await waitFor(() => expect(api.taglessFresh).toHaveBeenCalledTimes(1));
      expect(api.taglessFresh).toHaveBeenCalledWith(140, expect.objectContaining({ brand: 'Overture' }));
    });

    it('keeps a backend refusal on screen inside the dialog instead of toasting it away', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      (api.taglessFresh as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error('Slot has no tagless spool bound'),
      );
      render(<TaglessFreshModal context={makeContext()} usedGrams={820} manual onClose={onClose} />);

      await user.click(screen.getByRole('button', { name: /Mark as new roll/i }));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent('Slot has no tagless spool bound');
      // Dialog stays open so the operator can read the reason and retry.
      expect(onClose).not.toHaveBeenCalled();
      expect(mockShowToast).not.toHaveBeenCalledWith(expect.anything(), 'error');
    });
  });

  describe('prompt-driven open (default)', () => {
    it('keeps the consumption framing and the "Fresh roll" confirm', () => {
      render(<TaglessFreshModal context={makeContext()} onClose={vi.fn()} />);
      expect(screen.getByText(/Fresh roll on this slot\?/i)).toBeInTheDocument();
      expect(screen.getByText(/used past most of its label weight/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /Fresh roll/i })).toBeInTheDocument();
      expect(screen.queryByText(/Retire the record at/i)).not.toBeInTheDocument();
    });
  });
});
