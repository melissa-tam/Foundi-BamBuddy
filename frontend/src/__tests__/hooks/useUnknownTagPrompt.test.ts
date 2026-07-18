/**
 * Tests for useUnknownTagPrompt (quiet unknown-RFID-tag prompting):
 * - Raises a persistent toast (NOT a modal) on an `unknown-tag` event, with two actions
 * - "Add to Inventory" mints via the local endpoint when spoolman is OFF
 * - "Add to Inventory" mints via the Spoolman endpoint when spoolman is ON
 * - The dismiss action clears the slot with NO server call
 * - Dedupes repeat events for the same slot (one toast)
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useUnknownTagPrompt } from '../../hooks/useUnknownTagPrompt';
import type { UnknownTagDetail } from '../../hooks/useUnknownTagPrompt';
import { api } from '../../api/client';

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
  api: {
    createSpoolFromSlot: vi.fn().mockResolvedValue({}),
    createSpoolmanSpoolFromSlot: vi.fn().mockResolvedValue({}),
  },
}));

interface ToastAction {
  label: string;
  onClick: () => void;
}

function makeDetail(overrides: Partial<UnknownTagDetail> = {}): UnknownTagDetail {
  return {
    printer_id: 1,
    ams_id: 0,
    tray_id: 2,
    tag_uid: 'ABCD1234',
    tray_uuid: undefined,
    tray_type: 'PETG',
    tray_color: 'FF8800FF',
    tray_sub_brands: 'Overture',
    tray_count: 4,
    ...overrides,
  };
}

function createWrapper(settings?: { spoolman_enabled?: boolean }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  if (settings) queryClient.setQueryData(['settings'], settings);
  return ({ children }: { children: React.ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);
}

function dispatchTag(detail: UnknownTagDetail) {
  window.dispatchEvent(new CustomEvent('unknown-tag', { detail }));
}

function actionsOfLastToast(): ToastAction[] {
  const call = showPersistentToast.mock.calls.at(-1);
  return (call?.[3]?.actions ?? []) as ToastAction[];
}

describe('useUnknownTagPrompt', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.createSpoolFromSlot as ReturnType<typeof vi.fn>).mockResolvedValue({});
    (api.createSpoolmanSpoolFromSlot as ReturnType<typeof vi.fn>).mockResolvedValue({});
  });

  it('raises a persistent toast with two actions on an unknown-tag event', () => {
    renderHook(() => useUnknownTagPrompt(), { wrapper: createWrapper() });
    act(() => dispatchTag(makeDetail()));

    expect(showPersistentToast).toHaveBeenCalledTimes(1);
    const [id, message, type, options] = showPersistentToast.mock.calls[0];
    expect(id).toBe('unknown-tag-1-0-2');
    expect(message).toBe('inventory.unknownSpoolToast');
    expect(type).toBe('warning');
    expect(options.actions).toHaveLength(2);
  });

  it('"Add to Inventory" mints via the local endpoint when spoolman is off', async () => {
    renderHook(() => useUnknownTagPrompt(), { wrapper: createWrapper({ spoolman_enabled: false }) });
    act(() => dispatchTag(makeDetail()));

    await act(async () => {
      actionsOfLastToast()[0].onClick();
    });

    await waitFor(() =>
      expect(api.createSpoolFromSlot).toHaveBeenCalledWith({ printer_id: 1, ams_id: 0, tray_id: 2 }),
    );
    expect(api.createSpoolmanSpoolFromSlot).not.toHaveBeenCalled();
    // Slot cleared immediately on click (toast dismissed).
    expect(dismissToast).toHaveBeenCalledWith('unknown-tag-1-0-2');
  });

  it('"Add to Inventory" mints via the Spoolman endpoint when spoolman is on', async () => {
    renderHook(() => useUnknownTagPrompt(), { wrapper: createWrapper({ spoolman_enabled: true }) });
    act(() => dispatchTag(makeDetail()));

    await act(async () => {
      actionsOfLastToast()[0].onClick();
    });

    await waitFor(() =>
      expect(api.createSpoolmanSpoolFromSlot).toHaveBeenCalledWith({
        printer_id: 1,
        ams_id: 0,
        tray_id: 2,
      }),
    );
    expect(api.createSpoolFromSlot).not.toHaveBeenCalled();
  });

  it('the dismiss action clears the slot with no server call', () => {
    renderHook(() => useUnknownTagPrompt(), { wrapper: createWrapper() });
    act(() => dispatchTag(makeDetail()));

    act(() => {
      actionsOfLastToast()[1].onClick();
    });

    expect(dismissToast).toHaveBeenCalledWith('unknown-tag-1-0-2');
    expect(api.createSpoolFromSlot).not.toHaveBeenCalled();
    expect(api.createSpoolmanSpoolFromSlot).not.toHaveBeenCalled();
  });

  it('dedupes a repeat event for the same slot to a single toast', () => {
    renderHook(() => useUnknownTagPrompt(), { wrapper: createWrapper() });
    act(() => {
      dispatchTag(makeDetail());
      dispatchTag(makeDetail({ tray_sub_brands: 'Sunlu' }));
    });
    expect(showPersistentToast).toHaveBeenCalledTimes(1);
  });
});
