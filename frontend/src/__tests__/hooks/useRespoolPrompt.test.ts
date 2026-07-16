/**
 * Tests for useRespoolPrompt:
 * - Starts empty
 * - Enqueues on the `respool-prompt` window event
 * - Dedupes repeat events for the same slot
 * - Advances the queue (distinct slots) on dismiss
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useRespoolPrompt } from '../../hooks/useRespoolPrompt';
import type { RespoolPromptMessage } from '../../api/client';

// Auth-disabled deployment: isAuthed is true so the listener attaches.
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: null, authEnabled: false }),
}));

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

function dispatchPrompt(detail: RespoolPromptMessage) {
  window.dispatchEvent(new CustomEvent('respool-prompt', { detail }));
}

describe('useRespoolPrompt', () => {
  afterEach(() => vi.restoreAllMocks());

  it('starts with no prompt', () => {
    const { result } = renderHook(() => useRespoolPrompt());
    expect(result.current.prompt).toBeNull();
  });

  it('enqueues the payload on a respool-prompt event', () => {
    const { result } = renderHook(() => useRespoolPrompt());
    act(() => dispatchPrompt(makePrompt()));
    expect(result.current.prompt).not.toBeNull();
    expect(result.current.prompt?.printer_id).toBe(1);
    expect(result.current.prompt?.donor_spool_id).toBe(12);
    expect(result.current.prompt?.brand_prefill).toBe('Overture');
  });

  it('dedupes a repeat event for the same slot', () => {
    const { result } = renderHook(() => useRespoolPrompt());
    act(() => {
      dispatchPrompt(makePrompt());
      dispatchPrompt(makePrompt({ brand_prefill: 'Sunlu' }));
    });
    // Same slot → still the first payload, not the second.
    expect(result.current.prompt?.brand_prefill).toBe('Overture');
    // A single dismiss empties the queue (proving only one entry was queued).
    act(() => result.current.dismiss());
    expect(result.current.prompt).toBeNull();
  });

  it('queues distinct slots and advances on dismiss', () => {
    const { result } = renderHook(() => useRespoolPrompt());
    act(() => {
      dispatchPrompt(makePrompt({ tray_id: 0 }));
      dispatchPrompt(makePrompt({ tray_id: 1 }));
    });
    expect(result.current.prompt?.tray_id).toBe(0);
    act(() => result.current.dismiss());
    expect(result.current.prompt?.tray_id).toBe(1);
    act(() => result.current.dismiss());
    expect(result.current.prompt).toBeNull();
  });
});
