/**
 * Tests for ScheduledStartField (Phase 5): the ASAP/Schedule toggle emits a UTC
 * ISO string for a future time or null for ASAP, and reports validity (blocking
 * past/malformed entries). Uses iso/24h formats so the inputs are deterministic.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { ScheduledStartField } from '../../components/ScheduledStartField';

afterEach(() => vi.restoreAllMocks());

function setup(value: string | null = null) {
  const onChange = vi.fn();
  const onValidityChange = vi.fn();
  render(
    <ScheduledStartField
      value={value}
      onChange={onChange}
      onValidityChange={onValidityChange}
      dateFormat="iso"
      timeFormat="24h"
    />,
  );
  return { onChange, onValidityChange };
}

describe('ScheduledStartField', () => {
  it('defaults to ASAP with no date inputs', () => {
    setup(null);
    expect(screen.getByRole('button', { name: /start asap/i })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByLabelText(/date/i)).not.toBeInTheDocument();
    expect(screen.getByText(/starts as soon as an eligible printer is free/i)).toBeInTheDocument();
  });

  it('emits a future ISO string when switching to "Schedule for later"', async () => {
    const user = userEvent.setup();
    const { onChange, onValidityChange } = setup(null);

    await user.click(screen.getByRole('button', { name: /schedule for later/i }));

    // Date + time fields appear (labels prove the htmlFor association).
    expect(screen.getByLabelText(/^date$/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^time$/i)).toBeInTheDocument();

    // The seeded value (now+1h) is emitted as a future ISO string.
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(typeof last).toBe('string');
    expect(new Date(last as string).getTime()).toBeGreaterThan(Date.now());
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
  });

  it('rejects a past time: reports invalid and shows the error, no onChange with a past value', async () => {
    const user = userEvent.setup();
    const { onChange, onValidityChange } = setup(null);
    await user.click(screen.getByRole('button', { name: /schedule for later/i }));
    onChange.mockClear();

    const date = screen.getByLabelText(/^date$/i);
    await user.clear(date);
    await user.type(date, '2020-01-01');
    const time = screen.getByLabelText(/^time$/i);
    await user.clear(time);
    await user.type(time, '10:00');

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/pick a time in the future/i));
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
    // No emit carried the past time.
    for (const [arg] of onChange.mock.calls) {
      if (typeof arg === 'string') {
        expect(new Date(arg).getTime()).toBeGreaterThan(Date.now());
      }
    }
  });

  it('accepts a future time and clears the error', async () => {
    const user = userEvent.setup();
    const { onChange, onValidityChange } = setup(null);
    await user.click(screen.getByRole('button', { name: /schedule for later/i }));

    const date = screen.getByLabelText(/^date$/i);
    await user.clear(date);
    await user.type(date, '2099-12-31');
    const time = screen.getByLabelText(/^time$/i);
    await user.clear(time);
    await user.type(time, '10:00');

    await waitFor(() => expect(onValidityChange).toHaveBeenLastCalledWith(true));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    const last = onChange.mock.calls.at(-1)?.[0] as string;
    expect(new Date(last).getUTCFullYear()).toBe(2099);
  });

  it('switching back to ASAP emits null and is valid', async () => {
    const user = userEvent.setup();
    const { onChange, onValidityChange } = setup(null);
    await user.click(screen.getByRole('button', { name: /schedule for later/i }));
    await user.click(screen.getByRole('button', { name: /start asap/i }));

    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    expect(screen.queryByLabelText(/^date$/i)).not.toBeInTheDocument();
  });
});
