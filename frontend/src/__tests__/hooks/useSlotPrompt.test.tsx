/**
 * Tests for the shared per-slot prompt mechanics against the REAL ToastProvider,
 * pinning the operator-decided semantics of the toast X button:
 *
 *   X = "not now". It hides the toast; it does NOT answer the question. While
 *   the question is still live the backend keeps replaying it (WS re-broadcast
 *   or the `usePendingPromptSync` REST lane), and every replay must put the
 *   toast back — a prompt the operator X-ed away used to be unreachable for the
 *   life of that tab (the raise effect never re-ran for an already-queued slot).
 *
 * Also pins the other half: a replay while the toast is still visible must
 * refresh it in place, never stack a second toast for the same slot.
 */

import { describe, it, expect } from 'vitest';
import { render, screen, act, within } from '@testing-library/react';
import { useCallback } from 'react';
import { ToastProvider } from '../../contexts/ToastContext';
import { useSlotPrompt, type SlotPromptToast, type SlotTriple } from '../../hooks/useSlotPrompt';

const EVENT = 'test-slot-prompt';

interface TestPrompt extends SlotTriple {
  label: string;
}

/** Minimal consumer of the shared machinery: one persistent toast per queued
 *  slot, no actions (so the toast's only button is its X). */
function Harness() {
  const toPrompt = useCallback((detail: unknown): TestPrompt | null => {
    const d = detail as TestPrompt | null;
    return d && d.printer_id != null ? d : null;
  }, []);

  const renderToast = useCallback(
    (prompt: TestPrompt): SlotPromptToast => ({ message: prompt.label, actions: [] }),
    [],
  );

  useSlotPrompt<TestPrompt>({
    eventName: EVENT,
    toastIdPrefix: 'test-prompt',
    isAuthed: true,
    toPrompt,
    renderToast,
  });

  return null;
}

function dispatchPrompt(prompt: TestPrompt) {
  act(() => {
    window.dispatchEvent(new CustomEvent<TestPrompt>(EVENT, { detail: prompt }));
  });
}

function makePrompt(overrides: Partial<TestPrompt> = {}): TestPrompt {
  return { printer_id: 1, ams_id: 0, tray_id: 2, label: 'Fresh roll?', ...overrides };
}

function renderHarness() {
  return render(
    <ToastProvider>
      <Harness />
    </ToastProvider>,
  );
}

/** The X button of the toast carrying `message` (the only button when the
 *  prompt renders no actions). */
function dismissButtonFor(message: string): HTMLElement {
  const toast = screen.getByText(message).closest('div');
  if (!toast) throw new Error(`no toast found for "${message}"`);
  return within(toast).getByRole('button');
}

describe('useSlotPrompt — replay/re-raise semantics', () => {
  it('raises a persistent toast for a queued slot', () => {
    renderHarness();
    dispatchPrompt(makePrompt());

    expect(screen.getByText('Fresh roll?')).toBeInTheDocument();
  });

  it('re-raises a toast the operator X-ed away when the prompt is replayed', () => {
    renderHarness();
    dispatchPrompt(makePrompt());
    expect(screen.getByText('Fresh roll?')).toBeInTheDocument();

    // "Not now" — the operator hides the toast without answering.
    act(() => dismissButtonFor('Fresh roll?').click());
    expect(screen.queryByText('Fresh roll?')).not.toBeInTheDocument();

    // The question is still live, so the backend replays it (WS re-broadcast or
    // the REST recovery lane) — and the operator must see it again.
    dispatchPrompt(makePrompt());
    expect(screen.getByText('Fresh roll?')).toBeInTheDocument();
  });

  it('refreshes a still-visible toast on replay instead of stacking a second one', () => {
    renderHarness();
    dispatchPrompt(makePrompt());
    dispatchPrompt(makePrompt({ label: 'Fresh roll? (192 g left)' }));

    expect(screen.getAllByText('Fresh roll? (192 g left)')).toHaveLength(1);
    expect(screen.queryByText('Fresh roll?')).not.toBeInTheDocument();
  });

  it('keeps distinct slots on their own toasts', () => {
    renderHarness();
    dispatchPrompt(makePrompt());
    dispatchPrompt(makePrompt({ tray_id: 3, label: 'Fresh roll? (slot 4)' }));

    expect(screen.getByText('Fresh roll?')).toBeInTheDocument();
    expect(screen.getByText('Fresh roll? (slot 4)')).toBeInTheDocument();
  });

  it('a cross-client dismissal clears the slot for good — a replay is not re-raised', () => {
    render(
      <ToastProvider>
        <DismissibleHarness />
      </ToastProvider>,
    );
    dispatchPrompt(makePrompt());
    expect(screen.getByText('Fresh roll?')).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(
        new CustomEvent(`${EVENT}-dismissed`, {
          detail: { printer_id: 1, ams_id: 0, tray_id: 2 },
        }),
      );
    });
    expect(screen.queryByText('Fresh roll?')).not.toBeInTheDocument();
  });
});

/** Same harness, wired to the cross-client dismissal bridge. */
function DismissibleHarness() {
  const toPrompt = useCallback((detail: unknown): TestPrompt | null => {
    const d = detail as TestPrompt | null;
    return d && d.printer_id != null ? d : null;
  }, []);

  const renderToast = useCallback(
    (prompt: TestPrompt): SlotPromptToast => ({ message: prompt.label, actions: [] }),
    [],
  );

  useSlotPrompt<TestPrompt>({
    eventName: EVENT,
    dismissedEventName: `${EVENT}-dismissed`,
    toastIdPrefix: 'test-prompt',
    isAuthed: true,
    toPrompt,
    renderToast,
  });

  return null;
}
