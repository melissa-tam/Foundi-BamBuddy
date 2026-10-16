/**
 * MaintenanceBanner: the hold's scheduled return to service — "In maintenance
 * until <local time>" with Change (the shared return-time field) and Clear, or
 * "Set return time" when the hold has none. Behaviour through roles and labels.
 */
import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { MaintenanceBanner, type MaintenanceBannerProps } from '../../../components/maintenance/MaintenanceBanner';
import { formatDateTime } from '../../../utils/date';

const RELEASE_AT = '2099-12-31T10:00:00';

function setup(overrides: Partial<MaintenanceBannerProps> = {}) {
  const onExit = vi.fn();
  const onReschedule = vi.fn<(releaseAt: string | null) => Promise<unknown>>().mockResolvedValue({});
  render(
    <MaintenanceBanner
      printerId={7}
      printerName="009-H2S"
      hold={{ since: '2026-10-16T08:00:00', release_at: RELEASE_AT }}
      canControl
      exitPending={false}
      onExit={onExit}
      reschedulePending={false}
      onReschedule={onReschedule}
      dateFormat="iso"
      timeFormat="24h"
      {...overrides}
    />,
  );
  return { onExit, onReschedule };
}

describe('MaintenanceBanner', () => {
  it('names the return time in local time', () => {
    setup();
    expect(
      screen.getByText(`In maintenance until ${formatDateTime(RELEASE_AT, '24h')}`),
    ).toBeInTheDocument();
  });

  it('Clear sends a null return time', async () => {
    const user = userEvent.setup();
    const { onReschedule } = setup();

    await user.click(screen.getByRole('button', { name: /clear return time/i }));

    expect(onReschedule).toHaveBeenCalledWith(null);
  });

  it('Change opens the return-time field seeded with the current time and saves a new one', async () => {
    const user = userEvent.setup();
    const { onReschedule } = setup();

    await user.click(screen.getByRole('button', { name: /change return time/i }));

    const dialog = await screen.findByRole('dialog');
    const at = within(dialog).getByRole('button', { name: /at a set time/i });
    expect(at).toHaveAttribute('aria-pressed', 'true');
    const date = within(dialog).getByLabelText(/^date$/i);
    expect(date).toHaveValue('2099-12-31');

    await user.clear(date);
    await user.type(date, '2099-11-30');
    await user.click(within(dialog).getByRole('button', { name: /save/i }));

    await waitFor(() => expect(onReschedule).toHaveBeenCalledTimes(1));
    const sent = onReschedule.mock.calls[0][0];
    expect(typeof sent).toBe('string');
    expect(new Date(sent as string).getMonth()).toBe(10); // November, local
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('Manual in the Change dialog saves a cleared time', async () => {
    const user = userEvent.setup();
    const { onReschedule } = setup();

    await user.click(screen.getByRole('button', { name: /change return time/i }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /manual/i }));
    await user.click(within(dialog).getByRole('button', { name: /save/i }));

    await waitFor(() => expect(onReschedule).toHaveBeenCalledWith(null));
  });

  it('a hold with no return time offers Set return time and no Clear', () => {
    setup({ hold: { since: '2026-10-16T08:00:00', release_at: null } });

    expect(screen.queryByText(/in maintenance until/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /set return time/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /clear return time/i })).not.toBeInTheDocument();
  });

  it('Exit ends the hold now', async () => {
    const user = userEvent.setup();
    const { onExit } = setup();

    await user.click(screen.getByRole('button', { name: /exit maintenance mode/i }));

    expect(onExit).toHaveBeenCalledTimes(1);
  });

  it('every action is disabled without printers:control', () => {
    setup({ canControl: false });

    expect(screen.getByRole('button', { name: /exit maintenance mode/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /change return time/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /clear return time/i })).toBeDisabled();
  });

  it('compact shows the badge only', () => {
    setup({ compact: true });

    expect(screen.getByText(/^maintenance mode$/i)).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
