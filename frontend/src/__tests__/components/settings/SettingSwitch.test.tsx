/**
 * SettingSwitch: the shared Settings on/off row. Verifies the caption is the
 * switch's one accessible name (so a test can address it by role+name), that
 * toggling reports the NEW checked state, and that the mechanism copy is
 * reachable from the InfoHint tooltip rather than rendered inline.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../../utils';
import { SettingSwitch } from '../../../components/settings/SettingSwitch';

describe('SettingSwitch', () => {
  afterEach(cleanup);

  it('names the checkbox with the visible caption', () => {
    render(
      <SettingSwitch
        label="Chamber fan during cooldown"
        hint="Runs the chamber exhaust fan."
        checked
        onChange={() => {}}
      />,
    );

    const box = screen.getByRole('checkbox', { name: 'Chamber fan during cooldown' });
    expect((box as HTMLInputElement).checked).toBe(true);
    // The caption is visible text, not only an aria-label.
    expect(screen.getByText('Chamber fan during cooldown')).toBeInTheDocument();
  });

  it('reports the new state on toggle in both directions', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = render(
      <SettingSwitch label="Aux fan during cooldown" hint="h" checked onChange={onChange} />,
    );

    await user.click(screen.getByRole('checkbox', { name: 'Aux fan during cooldown' }));
    expect(onChange).toHaveBeenCalledWith(false);

    onChange.mockClear();
    rerender(
      <SettingSwitch
        label="Aux fan during cooldown"
        hint="h"
        checked={false}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole('checkbox', { name: 'Aux fan during cooldown' }));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it('carries the mechanism copy in a tooltip, never inline', async () => {
    const user = userEvent.setup();
    render(
      <SettingSwitch
        label="Aux fan during cooldown"
        hint="Runs the auxiliary fan until the eject dispatches."
        checked
        onChange={() => {}}
      />,
    );

    // Not on the primary surface (react-best-practices §9)...
    expect(screen.queryByText('Runs the auxiliary fan until the eject dispatches.')).toBeNull();
    // ...but reachable from the hint trigger.
    await user.click(
      screen.getByRole('button', { name: 'Runs the auxiliary fan until the eject dispatches.' }),
    );
    expect(await screen.findByRole('tooltip')).toHaveTextContent(
      'Runs the auxiliary fan until the eject dispatches.',
    );
  });
});
