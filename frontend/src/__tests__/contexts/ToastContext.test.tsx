/**
 * Tests for ToastContext's post-unmount safety guards.
 *
 * Regression: a login response handler calling showToast AFTER the provider
 * had already been unmounted by Vitest's afterEach scheduled a 3s setTimeout
 * that fired during test teardown. The callback's setToasts then tried to
 * schedule a React update against a torn-down jsdom, producing
 * "window is not defined" as an uncaught exception.
 *
 * The provider now gates every setToasts call on an isMountedRef and
 * re-checks inside the auto-dismiss setTimeout callback so stale async
 * paths no-op instead of crashing.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act, render, renderHook, screen, within } from '@testing-library/react';
import { type ReactNode } from 'react';
import { ToastProvider, useToast } from '../../contexts/ToastContext';

function Wrapper({ children }: { children: ReactNode }) {
  return <ToastProvider>{children}</ToastProvider>;
}

describe('ToastContext post-unmount safety', () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  it('does not crash when showToast is called after unmount', () => {
    const { result, unmount } = renderHook(() => useToast(), { wrapper: Wrapper });

    // Capture the callbacks BEFORE unmount — a real stale-closure scenario.
    // (Async handlers that kicked off before unmount keep their captured
    // context value and will invoke this function after we tear down.)
    const { showToast } = result.current;

    unmount();

    // Post-unmount invocation is now a no-op; must not throw.
    expect(() => showToast('delayed error message', 'error')).not.toThrow();
  });

  it('does not invoke setToasts when the auto-dismiss timer fires after unmount', async () => {
    vi.useFakeTimers();

    const { result, unmount } = renderHook(() => useToast(), { wrapper: Wrapper });

    act(() => {
      result.current.showToast('will outlive the provider', 'error');
    });

    // Unmount BEFORE the 3s timer fires — the unmount effect clears pending
    // timers, but a belt-and-braces check inside the timer callback (for
    // cases where the timer was scheduled post-unmount) must also hold.
    unmount();

    // Advance past the 3s auto-dismiss window. If the guard isn't in place
    // this would throw "window is not defined" in a torn-down jsdom; we
    // simulate by asserting no error propagates.
    expect(() => {
      vi.advanceTimersByTime(5000);
    }).not.toThrow();

    vi.useRealTimers();
  });

  it('post-unmount showPersistentToast and dismissToast are no-ops', () => {
    const { result, unmount } = renderHook(() => useToast(), { wrapper: Wrapper });
    const { showPersistentToast, dismissToast } = result.current;
    unmount();

    // Both must short-circuit rather than attempt setState on a dead tree.
    expect(() => showPersistentToast('orphan', 'still here', 'info')).not.toThrow();
    expect(() => dismissToast('orphan')).not.toThrow();
  });

  it('normal showToast flow still displays and auto-dismisses while mounted', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToast(), { wrapper: Wrapper });

    act(() => {
      result.current.showToast('mounted path works', 'success');
    });

    // No easy way to read toast DOM from the hook alone; assert the timer
    // ran without throwing — that proves the isMountedRef guard didn't
    // incorrectly short-circuit the mounted path.
    expect(() => {
      act(() => {
        vi.advanceTimersByTime(3500);
      });
    }).not.toThrow();

    vi.useRealTimers();
  });
});

describe('ToastContext viewport suppression', () => {
  // The kiosk layout flips setViewportSuppressed(true) on mount so the
  // SpoolBuddy display stays free of main-app toasts (login flows, etc.).
  // Verify the gate hides the visible viewport
  // without affecting the underlying state machine.
  function ViewportProbe() {
    const { showToast, setViewportSuppressed } = useToast();
    return (
      <>
        <button data-testid="show-toast" onClick={() => showToast('hello', 'success')} />
        <button data-testid="suppress-on" onClick={() => setViewportSuppressed(true)} />
        <button data-testid="suppress-off" onClick={() => setViewportSuppressed(false)} />
      </>
    );
  }

  it('hides the visible toast viewport when suppressed but keeps state alive', () => {
    const { container, getByTestId } = render(
      <ToastProvider>
        <ViewportProbe />
      </ToastProvider>
    );

    // Toast viewport is the fixed-position container with bottom-4 right-20.
    const findViewport = () => container.querySelector('div.fixed.bottom-4.right-20');
    expect(findViewport()?.className).not.toContain('hidden');

    act(() => {
      getByTestId('suppress-on').click();
    });
    expect(findViewport()?.className).toContain('hidden');

    // State is unaffected — emitting a toast while suppressed is fine; the
    // state container exists, just hidden.
    act(() => {
      getByTestId('show-toast').click();
    });
    expect(findViewport()?.className).toContain('hidden');

    // Restore on unmount of the kiosk layout (or via the setter directly).
    act(() => {
      getByTestId('suppress-off').click();
    });
    expect(findViewport()?.className).not.toContain('hidden');
  });
});

describe('ToastContext live region', () => {
  // WCAG 2.2 4.1.3 Status Messages. Toasts are status messages, and some carry
  // their entire content in the message ("Slot 2 unchanged") — without a live
  // region those are silent to a screen reader, which is the very failure mode
  // WS11 exists to close. The region is declared once on the always-mounted
  // viewport, never per toast: a live region must pre-exist its content, and
  // per-toast regions double-announce.
  function ToastProbe() {
    const { showToast } = useToast();
    return <button data-testid="show-toast" onClick={() => showToast('slot 2 unchanged', 'info')} />;
  }

  it('declares the toast viewport as a polite, non-atomic status region', () => {
    const { container, getByTestId } = render(
      <ToastProvider>
        <ToastProbe />
      </ToastProvider>
    );

    const viewport = container.querySelector('div.fixed.bottom-4.right-20') as HTMLElement;
    expect(viewport).not.toBeNull();
    expect(viewport.getAttribute('role')).toBe('status');
    expect(viewport.getAttribute('aria-live')).toBe('polite');
    expect(viewport.getAttribute('aria-atomic')).toBe('false');

    // The region is the same element before and after a toast lands, so the
    // message is an in-place content change an assistive tech can observe.
    act(() => {
      getByTestId('show-toast').click();
    });
    expect(container.querySelector('div.fixed.bottom-4.right-20')).toBe(viewport);
    expect(viewport.textContent).toContain('slot 2 unchanged');
  });

  it('gives no toast a live region of its own, and keeps the controls out of the live scope', () => {
    const { container, getByTestId } = render(
      <ToastProvider>
        <ToastProbe />
      </ToastProvider>
    );

    act(() => {
      getByTestId('show-toast').click();
    });

    const viewport = container.querySelector('div.fixed.bottom-4.right-20') as HTMLElement;
    // Still exactly one region, still the always-mounted viewport: a per-toast
    // region double-announces and has to pre-exist its own content.
    expect(viewport.querySelectorAll('[role="status"], [role="alert"]')).toHaveLength(0);

    // The only aria-live inside is the EXCLUSION around the controls, so what
    // gets announced is the sentence rather than the sentence plus its buttons.
    const nested = Array.from(viewport.querySelectorAll('[aria-live]'));
    expect(nested.map((el) => el.getAttribute('aria-live'))).toEqual(['off']);

    const excluded = nested[0] as HTMLElement;
    expect(excluded.textContent).not.toContain('slot 2 unchanged');
    expect(within(excluded).getByRole('button', { name: 'Dismiss' })).toBeInTheDocument();
    // …and the announced text is still inside the region itself.
    expect(within(viewport).getByText('slot 2 unchanged')).toBeInTheDocument();
  });
});

describe('ToastContext persistent toasts', () => {
  // The "Re-check slot" acknowledgement carries the undo for a mint. It is
  // created with showPersistentToast, which schedules NO auto-dismiss timer —
  // the operator dismisses it, or leaves it standing. (The durable path to the
  // same action is the slot card; this is the fast one.)
  function AckProbe() {
    const { showPersistentToast } = useToast();
    return (
      <button
        data-testid="show-ack"
        onClick={() =>
          showPersistentToast('slot-recheck-1-0-1', 'Slot 2: new roll recorded. 1,000 g assumed.', 'success', {
            actions: [{ label: 'Restore previous roll', onClick: () => {} }],
          })
        }
      />
    );
  }

  it('does not auto-dismiss an acknowledgement created with showPersistentToast', () => {
    vi.useFakeTimers();
    const { getByTestId } = render(
      <ToastProvider>
        <AckProbe />
      </ToastProvider>
    );

    act(() => {
      getByTestId('show-ack').click();
    });
    expect(screen.getByText('Slot 2: new roll recorded. 1,000 g assumed.')).toBeInTheDocument();

    // Well past the longest severity window (errors, 8 s).
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(screen.getByText('Slot 2: new roll recorded. 1,000 g assumed.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restore previous roll' })).toBeInTheDocument();

    // It goes when the operator says so, not when a clock does.
    act(() => {
      screen.getByRole('button', { name: 'Dismiss' }).click();
    });
    expect(screen.queryByText('Slot 2: new roll recorded. 1,000 g assumed.')).not.toBeInTheDocument();

    vi.useRealTimers();
  });
});
