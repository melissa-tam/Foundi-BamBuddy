/**
 * The New roll dialog's label field on a TAGLESS row opens on the
 * `default_spool_weight_g` setting (the figure the replacement mints at when
 * none is typed). A tagged row keeps the server's own prefill.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { NewRollModal } from '../../components/NewRollModal';
import type { NewRollContext } from '../../utils/newRollContext';
import { api } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    newRoll: vi.fn(),
    getSpools: vi.fn().mockResolvedValue([]),
    dismissRespoolPrompt: vi.fn(),
    getSettings: vi.fn(),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
  },
}));

vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: vi.fn() }) };
});

function context(overrides: Partial<NewRollContext> = {}): NewRollContext {
  return {
    printer_id: 7,
    ams_id: 0,
    tray_id: 2,
    spool_id: 140,
    tagged: false,
    origin: 'manual',
    material: 'PETG',
    rgba: '000000FF',
    used_g: 820,
    ...overrides,
  };
}

describe('NewRollModal label weight prefill', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getSettings as ReturnType<typeof vi.fn>).mockResolvedValue({ default_spool_weight_g: 1100 });
    (api.newRoll as ReturnType<typeof vi.fn>).mockResolvedValue({ id: 201 });
  });

  it('opens a tagless row on the default-weight setting and submits it', async () => {
    const user = userEvent.setup();
    render(<NewRollModal context={context()} onClose={vi.fn()} />);

    const field = screen.getByLabelText('Label weight (g)');
    await waitFor(() => expect(field).toHaveValue(1100));

    await user.click(screen.getByRole('button', { name: /Mark as new roll/i }));
    await waitFor(() =>
      expect(api.newRoll).toHaveBeenCalledWith(140, expect.objectContaining({ label_weight: 1100 })),
    );
  });

  it('keeps an operator figure typed before the setting resolves', async () => {
    let resolveSettings: (value: unknown) => void = () => {};
    const settings = new Promise((resolve) => {
      resolveSettings = resolve;
    });
    (api.getSettings as ReturnType<typeof vi.fn>).mockReturnValue(settings);
    const user = userEvent.setup();
    render(<NewRollModal context={context()} onClose={vi.fn()} />);

    const field = screen.getByLabelText('Label weight (g)');
    await user.type(field, '900');
    await act(async () => {
      resolveSettings({ default_spool_weight_g: 1100 });
      await settings;
      await new Promise((tick) => setTimeout(tick, 50));
    });

    expect(field).toHaveValue(900);
  });

  it("keeps a tagged row's server prefill", async () => {
    render(
      <NewRollModal
        context={context({ tagged: true, spool_id: 12, tag_identity: 'DEADBEEF', label_weight_prefill: 1000 })}
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    expect(screen.getByLabelText('Label weight (g)')).toHaveValue(1000);
  });
});
