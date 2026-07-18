/**
 * Tests for the shared InfoHint tooltip primitive.
 *
 * The value of promoting InfoHint over the old hover-only native `title=` is
 * the keyboard + touch behaviour, so that is what these assertions pin down:
 * the trigger is a real, tab-reachable button; the tooltip appears on focus,
 * hover and tap; it hides on blur, Escape and an outside press; and while
 * visible it is linked to the trigger via `aria-describedby`.
 */

import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { InfoHint } from '../../components/ui/InfoHint';

const TEXT = 'Bed temperature the print must cool to before the sweep starts.';

describe('InfoHint', () => {
  it('renders a labelled button trigger and no tooltip initially', () => {
    render(<InfoHint text={TEXT} />);

    expect(screen.getByRole('button', { name: TEXT })).toBeInTheDocument();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('is reachable in the tab order', async () => {
    const user = userEvent.setup();
    render(<InfoHint text={TEXT} />);

    const trigger = screen.getByRole('button', { name: TEXT });
    expect(trigger).not.toHaveFocus();

    await user.tab();

    expect(trigger).toHaveFocus();
  });

  it('shows the tooltip on focus and hides it on blur', async () => {
    const user = userEvent.setup();
    render(
      <>
        <InfoHint text={TEXT} />
        <button type="button">next</button>
      </>,
    );

    await user.tab(); // focus the InfoHint trigger
    expect(await screen.findByRole('tooltip')).toHaveTextContent(TEXT);

    await user.tab(); // move focus to the sibling button -> blur
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('links the visible tooltip to the trigger via aria-describedby', async () => {
    const user = userEvent.setup();
    render(<InfoHint text={TEXT} />);

    await user.tab();

    const trigger = screen.getByRole('button', { name: TEXT });
    const tooltip = await screen.findByRole('tooltip');
    const describedBy = trigger.getAttribute('aria-describedby');

    expect(describedBy).toBeTruthy();
    expect(tooltip).toHaveAttribute('id', describedBy);
  });

  it('shows the tooltip on hover and hides it on unhover', async () => {
    const user = userEvent.setup();
    render(<InfoHint text={TEXT} />);

    const trigger = screen.getByRole('button', { name: TEXT });

    await user.hover(trigger);
    expect(await screen.findByRole('tooltip')).toBeInTheDocument();

    await user.unhover(trigger);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('hides the tooltip when Escape is pressed', async () => {
    const user = userEvent.setup();
    render(<InfoHint text={TEXT} />);

    await user.tab();
    expect(await screen.findByRole('tooltip')).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('toggles the tooltip on a touch tap (no hover)', () => {
    // A real touch tap fires pointerdown → click with no mouse-hover events,
    // so the tap-toggle path is exercised in isolation. userEvent.click would
    // simulate a hovering mouse pointer, which is a different affordance.
    render(<InfoHint text={TEXT} />);

    const trigger = screen.getByRole('button', { name: TEXT });

    fireEvent.pointerDown(trigger, { pointerType: 'touch' });
    fireEvent.click(trigger);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();

    fireEvent.pointerDown(trigger, { pointerType: 'touch' });
    fireEvent.click(trigger);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });

  it('shows the tooltip when the trigger is clicked with a mouse', async () => {
    const user = userEvent.setup();
    render(<InfoHint text={TEXT} />);

    await user.click(screen.getByRole('button', { name: TEXT }));
    expect(await screen.findByRole('tooltip')).toBeInTheDocument();
  });

  it('hides the tooltip on an outside pointer press', async () => {
    const user = userEvent.setup();
    render(
      <>
        <InfoHint text={TEXT} />
        <button type="button">outside</button>
      </>,
    );

    await user.click(screen.getByRole('button', { name: TEXT }));
    expect(await screen.findByRole('tooltip')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'outside' }));
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
  });
});
