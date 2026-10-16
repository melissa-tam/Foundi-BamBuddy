/**
 * EnterMaintenanceDialog: what the hold will stop, plus the optional return to
 * service. Manual (the default) enters with no time; "At a set time" enters
 * with a future UTC ISO time; a past time blocks the confirm.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { EnterMaintenanceDialog } from '../../../components/maintenance/EnterMaintenanceDialog';

function setup() {
  const onConfirm = vi.fn<(releaseAt: string | null) => void>();
  const onCancel = vi.fn();
  render(
    <EnterMaintenanceDialog
      printerId={7}
      printerName="009-H2S"
      effects={['• Stop the eject sweep — the plate stays gated']}
      pending={false}
      onConfirm={onConfirm}
      onCancel={onCancel}
      dateFormat="iso"
      timeFormat="24h"
    />,
  );
  return { onConfirm, onCancel };
}

const confirmButton = () => screen.getByRole('button', { name: /^enter maintenance mode$/i });

describe('EnterMaintenanceDialog', () => {
  it('lists what the hold stops and defaults to Manual', () => {
    setup();
    expect(screen.getByRole('dialog')).toHaveTextContent(/stop the eject sweep/i);
    expect(screen.getByRole('button', { name: /manual/i })).toHaveAttribute('aria-pressed', 'true');
  });

  it('Manual enters with no return time', async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup();

    await user.click(confirmButton());

    expect(onConfirm).toHaveBeenCalledWith(null);
  });

  it('a set time enters with a future UTC ISO time', async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup();

    await user.click(screen.getByRole('button', { name: /at a set time/i }));
    const date = screen.getByLabelText(/^date$/i);
    await user.clear(date);
    await user.type(date, '2099-12-31');
    const time = screen.getByLabelText(/^time$/i);
    await user.clear(time);
    await user.type(time, '10:00');
    await user.click(confirmButton());

    expect(onConfirm).toHaveBeenCalledTimes(1);
    const sent = onConfirm.mock.calls[0][0];
    expect(sent).toMatch(/Z$/);
    expect(new Date(sent as string).getFullYear()).toBe(2099);
  });

  it('a past time blocks the confirm', async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup();

    await user.click(screen.getByRole('button', { name: /at a set time/i }));
    const date = screen.getByLabelText(/^date$/i);
    await user.clear(date);
    await user.type(date, '2020-01-01');

    await waitFor(() => expect(confirmButton()).toBeDisabled());
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
