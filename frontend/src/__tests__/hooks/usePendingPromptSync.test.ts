/**
 * Tests for usePendingPromptSync — the REST recovery lane for the per-slot
 * operator prompts. The websocket broadcast is fire-and-forget, so a tab that
 * was not connected when a prompt fired never learned about it (prod 2026-07-24:
 * a "Fresh roll?" prompt reached zero clients). This hook re-asks the backend
 * and replays what is still live through the same window events the WS bridge
 * dispatches:
 * - fetches on mount
 * - fetches again on every `ws-connected` event
 * - re-dispatches entries as `tagless-fresh-prompt` / `respool-prompt` with the
 *   SAME detail shape `useWebSocket` produces
 * - coalesces a reconnect storm behind an in-flight guard (one fetch)
 * - swallows fetch errors (a recovery lane must never break the shell)
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { usePendingPromptSync } from '../../hooks/usePendingPromptSync';
import { api } from '../../api/client';
import type {
  PendingPromptsResponse,
  RespoolPromptMessage,
  TaglessFreshPromptMessage,
} from '../../api/client';

// Auth-disabled deployment: isAuthed is true so the hook syncs.
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: null, authEnabled: false }),
}));

vi.mock('../../api/client', () => ({
  api: { getPendingPrompts: vi.fn() },
}));

const getPendingPrompts = api.getPendingPrompts as ReturnType<typeof vi.fn>;

const FRESH: TaglessFreshPromptMessage = {
  printer_id: 4,
  ams_id: 0,
  tray_id: 0,
  spool_id: 51,
  remaining_g: 192,
  material: 'PETG',
  rgba: '000000FF',
};

const RESPOOL: RespoolPromptMessage = {
  printer_id: 2,
  ams_id: 0,
  tray_id: 3,
  tag_uid: 'ABC123',
  tray_uuid: 'uuid-1',
  tray_type: 'PETG',
  tray_color: '000000FF',
  tray_sub_brands: 'PETG HF',
  tray_count: 1,
  donor_spool_id: 12,
  donor_remaining_g: 41,
  brand_prefill: 'Bambu Lab',
  label_weight_prefill: 1000,
};

const EMPTY: PendingPromptsResponse = { fresh: [], respool: [] };

let listeners: Array<() => void> = [];

/** Collects the details of every prompt window event the hook re-dispatches. */
function captureEvents(name: string): unknown[] {
  const seen: unknown[] = [];
  const handler = (e: Event) => seen.push((e as CustomEvent<unknown>).detail);
  window.addEventListener(name, handler);
  listeners.push(() => window.removeEventListener(name, handler));
  return seen;
}

describe('usePendingPromptSync', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getPendingPrompts.mockResolvedValue(EMPTY);
  });

  afterEach(() => {
    listeners.forEach(off => off());
    listeners = [];
  });

  it('fetches the pending prompts on mount', async () => {
    renderHook(() => usePendingPromptSync());
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(1));
  });

  it('fetches again on every ws-connected event (the reconnect gap)', async () => {
    renderHook(() => usePendingPromptSync());
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(1));

    await act(async () => {
      window.dispatchEvent(new Event('ws-connected'));
    });
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(2));

    await act(async () => {
      window.dispatchEvent(new Event('ws-connected'));
    });
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(3));
  });

  it('replays each entry through the WS window-event bridge with the bridge detail shape', async () => {
    const fresh = captureEvents('tagless-fresh-prompt');
    const respool = captureEvents('respool-prompt');
    getPendingPrompts.mockResolvedValue({ fresh: [FRESH], respool: [RESPOOL] });

    renderHook(() => usePendingPromptSync());

    // The detail must be field-for-field what `useWebSocket` bridges, so the
    // prompt hooks cannot tell a replay from a live broadcast.
    await waitFor(() => expect(fresh).toHaveLength(1));
    expect(fresh[0]).toEqual({
      printer_id: 4,
      ams_id: 0,
      tray_id: 0,
      spool_id: 51,
      remaining_g: 192,
      material: 'PETG',
      rgba: '000000FF',
    });

    await waitFor(() => expect(respool).toHaveLength(1));
    expect(respool[0]).toEqual({
      printer_id: 2,
      ams_id: 0,
      tray_id: 3,
      tag_uid: 'ABC123',
      tray_uuid: 'uuid-1',
      tray_type: 'PETG',
      tray_color: '000000FF',
      tray_sub_brands: 'PETG HF',
      tray_count: 1,
      donor_spool_id: 12,
      donor_remaining_g: 41,
      brand_prefill: 'Bambu Lab',
      label_weight_prefill: 1000,
    });
  });

  it('replays every entry when several prompts are live at once', async () => {
    const fresh = captureEvents('tagless-fresh-prompt');
    getPendingPrompts.mockResolvedValue({
      fresh: [FRESH, { ...FRESH, printer_id: 9, tray_id: 2, spool_id: 77 }],
      respool: [],
    });

    renderHook(() => usePendingPromptSync());

    await waitFor(() => expect(fresh).toHaveLength(2));
    expect((fresh[1] as TaglessFreshPromptMessage).spool_id).toBe(77);
  });

  it('coalesces a reconnect storm behind the in-flight guard (one fetch)', async () => {
    let settle: ((value: PendingPromptsResponse) => void) | undefined;
    getPendingPrompts.mockImplementation(
      () =>
        new Promise<PendingPromptsResponse>(resolve => {
          settle = resolve;
        }),
    );

    renderHook(() => usePendingPromptSync());
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(1));

    act(() => {
      window.dispatchEvent(new Event('ws-connected'));
      window.dispatchEvent(new Event('ws-connected'));
      window.dispatchEvent(new Event('ws-connected'));
    });
    expect(getPendingPrompts).toHaveBeenCalledTimes(1);

    // …and the guard releases once the fetch settles, so the NEXT reconnect syncs.
    await act(async () => {
      settle?.(EMPTY);
    });
    await act(async () => {
      window.dispatchEvent(new Event('ws-connected'));
    });
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(2));
  });

  it('swallows a failed fetch — no throw, no events, and the lane stays armed', async () => {
    const fresh = captureEvents('tagless-fresh-prompt');
    getPendingPrompts.mockRejectedValue(new Error('HTTP 503'));

    renderHook(() => usePendingPromptSync());
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(1));
    expect(fresh).toHaveLength(0);

    getPendingPrompts.mockResolvedValue({ fresh: [FRESH], respool: [] });
    await act(async () => {
      window.dispatchEvent(new Event('ws-connected'));
    });
    await waitFor(() => expect(fresh).toHaveLength(1));
  });

  it('stops syncing once unmounted', async () => {
    const { unmount } = renderHook(() => usePendingPromptSync());
    await waitFor(() => expect(getPendingPrompts).toHaveBeenCalledTimes(1));

    unmount();
    await act(async () => {
      window.dispatchEvent(new Event('ws-connected'));
    });
    expect(getPendingPrompts).toHaveBeenCalledTimes(1);
  });
});
