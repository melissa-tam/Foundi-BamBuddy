/**
 * Tests for ToastContext's severity-based auto-dismiss durations.
 *
 * Errors linger longest (8s) so an operator glancing away does not miss a
 * failure; warnings sit in between (5s); success/info keep the original 3s.
 * These assertions pin the per-severity windows via fake timers.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { ToastProvider, useToast } from '../../contexts/ToastContext';

function Emitter() {
  const { showToast } = useToast();
  return (
    <>
      <button data-testid="emit-error" onClick={() => showToast('boom failed', 'error')} />
      <button data-testid="emit-success" onClick={() => showToast('all good', 'success')} />
      <button data-testid="emit-warning" onClick={() => showToast('heads up', 'warning')} />
    </>
  );
}

function renderProvider() {
  return render(
    <ToastProvider>
      <Emitter />
    </ToastProvider>,
  );
}

describe('ToastContext severity-based durations', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('keeps an error toast visible at 3.5s and dismisses it by 8.5s', () => {
    renderProvider();

    act(() => {
      screen.getByTestId('emit-error').click();
    });
    expect(screen.getByText('boom failed')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3500);
    });
    expect(screen.getByText('boom failed')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5000); // total 8.5s, past the 8s error window
    });
    expect(screen.queryByText('boom failed')).not.toBeInTheDocument();
  });

  it('dismisses a success toast by 3.5s', () => {
    renderProvider();

    act(() => {
      screen.getByTestId('emit-success').click();
    });
    expect(screen.getByText('all good')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3500); // past the 3s success window
    });
    expect(screen.queryByText('all good')).not.toBeInTheDocument();
  });

  it('keeps a warning toast visible at 3.5s and dismisses it by 5.5s', () => {
    renderProvider();

    act(() => {
      screen.getByTestId('emit-warning').click();
    });
    expect(screen.getByText('heads up')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3500);
    });
    expect(screen.getByText('heads up')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(2000); // total 5.5s, past the 5s warning window
    });
    expect(screen.queryByText('heads up')).not.toBeInTheDocument();
  });
});
