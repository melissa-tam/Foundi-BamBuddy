/**
 * Tests for useRespoolPrompt (quiet, ask-once re-spool prompting):
 * - Starts with no open modal
 * - Raises a persistent toast (NOT the modal) on a `respool-prompt` event
 * - Dedupes repeat events for the same slot (one toast)
 * - "Same spool" POSTs the dismissal with the slot triple, then clears the toast
 * - The `respool-prompt-dismissed` window event clears the matching slot
 * - "Review…" opens the modal; closeModal clears it
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useRespoolPrompt } from '../../hooks/useRespoolPrompt';
import { api } from '../../api/client';
import type { RespoolPromptMessage } from '../../api/client';

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
  api: { dismissRespoolPrompt: vi.fn().mockResolvedValue({}) },
}));

interface ToastAction {
  label: string;
  onClick: () => void;
}

function makePrompt(overrides: Partial<RespoolPromptMessage> = {}): RespoolPromptMessage {
  return {
    printer_id: 1,
    ams_id: 0,
    tray_id: 2,
    tag_uid: 'ABCD1234',
    tray_uuid: null,
    tray_type: 'PETG',
    tray_color: 'FF8800FF',
    tray_sub_brands: 'PETG HF',
    tray_count: 4,
    donor_spool_id: 12,
    donor_remaining_g: 18,
    brand_prefill: 'Overture',
    label_weight_prefill: 1000,
    ...overrides,
  };
}

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

function dispatchPrompt(detail: RespoolPromptMessage) {
  window.dispatchEvent(new CustomEvent('respool-prompt', { detail }));
}

function actionsOfLastToast(): ToastAction[] {
  const call = showPersistentToast.mock.calls.at(-1);
  return (call?.[3]?.actions ?? []) as ToastAction[];
}

describe('useRespoolPrompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.dismissRespoolPrompt as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it('starts with no open modal', () => {
    const { result } = renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    expect(result.current.activeContext).toBeNull();
  });

  it('raises a persistent toast — not the modal — on a respool-prompt event', () => {
    const { result } = renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    expect(showPersistentToast).toHaveBeenCalledTimes(1);
    const [id, message, type, options] = showPersistentToast.mock.calls[0];
    expect(id).toBe('respool-1-0-2');
    expect(message).toBe('inventory.respool.reusedTagToast');
    expect(type).toBe('warning');
    expect(options.actions).toHaveLength(2);
    // The modal stays closed — the toast is the surface now.
    expect(result.current.activeContext).toBeNull();
  });

  it('words a near_empty prompt as a roll replacement and appends the remaining grams', () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt({ trigger: 'near_empty', donor_remaining_g: 18 })));

    const [, message] = showPersistentToast.mock.calls[0];
    // Roll-replacement framing (not a detected reused tag), with the grams clause appended.
    expect(message).toBe('inventory.respool.nearEmptyToast inventory.respool.nearEmptyToastRemaining');
  });

  it('words a spent prompt with its age and appends the provenance numbers', () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() =>
      dispatchPrompt(
        makePrompt({ trigger: 'spent', spent_age_s: 7200, ams_remain_pct: 100, ledger_remain_pct: 4 }),
      ),
    );

    const [, message] = showPersistentToast.mock.calls[0];
    // The age-bearing spent copy, plus the AMS-vs-ledger numbers clause.
    expect(message).toBe('inventory.respool.spentToast inventory.respool.spentToastNumbers');
  });

  it('falls back to the reused-tag wording for a spent prompt with no age', () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt({ trigger: 'spent' }))); // spent_age_s absent

    const [, message] = showPersistentToast.mock.calls[0];
    expect(message).toBe('inventory.respool.reusedTagToast');
  });

  it('keeps the reused-tag wording for spent and remain_jump prompts', () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => {
      dispatchPrompt(makePrompt({ trigger: 'spent' }));
      dispatchPrompt(makePrompt({ tray_id: 3, trigger: 'remain_jump' }));
    });

    const messages = showPersistentToast.mock.calls.map(call => call[1]);
    expect(messages).toEqual([
      'inventory.respool.reusedTagToast',
      'inventory.respool.reusedTagToast',
    ]);
  });

  it('dedupes a repeat event for the same slot to a single toast', () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => {
      dispatchPrompt(makePrompt());
      dispatchPrompt(makePrompt({ brand_prefill: 'Sunlu' }));
    });
    expect(showPersistentToast).toHaveBeenCalledTimes(1);
  });

  it('"Same spool" POSTs the dismissal with the slot triple and clears the toast', async () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    await act(async () => {
      actionsOfLastToast()[0].onClick();
    });

    expect(api.dismissRespoolPrompt).toHaveBeenCalledWith(12, {
      printer_id: 1,
      ams_id: 0,
      tray_id: 2,
    });
    await waitFor(() => expect(dismissToast).toHaveBeenCalledWith('respool-1-0-2'));
  });

  it('clears a slot on the respool-prompt-dismissed window event (cross-client sync)', () => {
    renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    act(() => {
      window.dispatchEvent(
        new CustomEvent('respool-prompt-dismissed', {
          detail: { printer_id: 1, ams_id: 0, tray_id: 2 },
        }),
      );
    });

    expect(dismissToast).toHaveBeenCalledWith('respool-1-0-2');
  });

  it('"Review…" opens the modal, and closeModal clears it', () => {
    const { result } = renderHook(() => useRespoolPrompt(), { wrapper: createWrapper() });
    act(() => dispatchPrompt(makePrompt()));

    act(() => {
      actionsOfLastToast()[1].onClick();
    });
    expect(result.current.activeContext?.printer_id).toBe(1);
    expect(result.current.activeContext?.tray_id).toBe(2);
    expect(dismissToast).toHaveBeenCalledWith('respool-1-0-2');

    act(() => result.current.closeModal());
    expect(result.current.activeContext).toBeNull();
  });
});
