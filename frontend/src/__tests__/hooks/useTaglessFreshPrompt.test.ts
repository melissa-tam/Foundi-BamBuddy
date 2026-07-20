/**
 * Tests for useTaglessFreshPrompt (tagless fresh-roll prompting), mirroring
 * useRespoolPrompt's tests — the two hooks share the `useSlotPrompt` mechanics:
 * - Starts with no open modal
 * - Raises a persistent toast (NOT a modal) on a `tagless-fresh-prompt` event
 * - Dedupes repeat events for the same slot (one toast)
 * - "Same roll" POSTs answer:"same" with the spool id + slot triple, then clears
 * - The `tagless-fresh-prompt-dismissed` window event clears the matching slot
 * - "Review…" opens the modal; closeModal clears it
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useTaglessFreshPrompt } from '../../hooks/useTaglessFreshPrompt';
import { api } from '../../api/client';
import type { TaglessFreshPromptMessage } from '../../api/client';

// Auth-disabled deployment: isAuthed is true so the listener attaches.
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: null, authEnabled: false }),
}));

const showPersistentToast = vi.fn();
const dismissToast = vi.fn();
const showToast = vi.fn();
vi.mock('../../contexts/ToastContext', () => ({
  useToast: () => ({ showPersistentToast, dismissToast, showToast }),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: {} }),
}));

vi.mock('../../api/client', () => ({
  api: { taglessFresh: vi.fn().mockResolvedValue({}) },
}));

interface ToastAction {
  label: string;
  onClick: () => void;
}

function makePrompt(overrides: Partial<TaglessFreshPromptMessage> = {}): TaglessFreshPromptMessage {
  return {
    printer_id: 1,
    ams_id: 0,
    tray_id: 2,
    spool_id: 55,
    remaining_g: 420,
    material: 'PETG',
    rgba: 'FF8800FF',
    ...overrides,
  };
}

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

function dispatchPrompt(detail: TaglessFreshPromptMessage) {
  window.dispatchEvent(new CustomEvent('tagless-fresh-prompt', { detail }));
}

function actionsOfLastToast(): ToastAction[] {
  const call = showPersistentToast.mock.calls.at(-1);
  return (call?.[3]?.actions ?? []) as ToastAction[];
}

describe('useTaglessFreshPrompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.taglessFresh as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it('starts with no open modal', () => {
    const { result } = renderHook(() => useTaglessFreshPrompt(), { wrapper: createWrapper() });
    expect(result.current.activeContext).toBeNull();
  });

  it('raises a persistent toast — not the modal — on a tagless-fresh-prompt event', () => {
    const { result } = renderHook(() => useTaglessFreshPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    expect(showPersistentToast).toHaveBeenCalledTimes(1);
    const [id, message, type, options] = showPersistentToast.mock.calls[0];
    expect(id).toBe('tagless-fresh-1-0-2');
    expect(message).toBe('inventory.freshRoll.promptToast');
    expect(type).toBe('warning');
    expect(options.actions).toHaveLength(2);
    expect(result.current.activeContext).toBeNull();
  });

  it('dedupes a repeat event for the same slot to a single toast', () => {
    renderHook(() => useTaglessFreshPrompt(), { wrapper: createWrapper() });
    act(() => {
      dispatchPrompt(makePrompt());
      dispatchPrompt(makePrompt({ remaining_g: 300 }));
    });
    expect(showPersistentToast).toHaveBeenCalledTimes(1);
  });

  it('"Same roll" POSTs answer:"same" with the spool id + slot triple and clears the toast', async () => {
    renderHook(() => useTaglessFreshPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    await act(async () => {
      actionsOfLastToast()[0].onClick();
    });

    expect(api.taglessFresh).toHaveBeenCalledWith(55, {
      printer_id: 1,
      ams_id: 0,
      tray_id: 2,
      answer: 'same',
    });
    await waitFor(() => expect(dismissToast).toHaveBeenCalledWith('tagless-fresh-1-0-2'));
  });

  it('clears a slot on the tagless-fresh-prompt-dismissed window event (cross-client sync)', () => {
    renderHook(() => useTaglessFreshPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    act(() => {
      window.dispatchEvent(
        new CustomEvent('tagless-fresh-prompt-dismissed', {
          detail: { printer_id: 1, ams_id: 0, tray_id: 2 },
        }),
      );
    });

    expect(dismissToast).toHaveBeenCalledWith('tagless-fresh-1-0-2');
  });

  it('"Review…" opens the modal, and closeModal clears it', () => {
    const { result } = renderHook(() => useTaglessFreshPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    act(() => {
      actionsOfLastToast()[1].onClick();
    });
    expect(result.current.activeContext?.printer_id).toBe(1);
    expect(result.current.activeContext?.tray_id).toBe(2);
    expect(result.current.activeContext?.spool_id).toBe(55);
    expect(dismissToast).toHaveBeenCalledWith('tagless-fresh-1-0-2');

    act(() => result.current.closeModal());
    expect(result.current.activeContext).toBeNull();
  });
});
