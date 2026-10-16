/**
 * SpoolBuddy "Add to Inventory" quick-add (internal inventory) creates the roll at
 * the `default_spool_weight_g` setting (`hooks/useDefaultSpoolWeight`), never a
 * client literal. The quick-add dialog has no weight field, so there is no
 * operator figure on this surface; before the setting resolves it sends 0, which
 * the backend owner (`services/spool_defaults`) reads as "no figure" and fills
 * from the same setting.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, Outlet } from 'react-router-dom';
import { SpoolBuddyDashboard } from '../../pages/spoolbuddy/SpoolBuddyDashboard';
import { ToastProvider } from '../../contexts/ToastContext';
import { api } from '../../api/client';

vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: vi.fn() }) };
});

vi.mock('../../api/client', () => ({
  api: {
    getSettings: vi.fn(),
    getSpools: vi.fn().mockResolvedValue([]),
    getPrinters: vi.fn().mockResolvedValue([]),
    getPrinterStatus: vi.fn().mockResolvedValue({ connected: false }),
    getSpoolmanSettings: vi.fn().mockResolvedValue({
      spoolman_enabled: 'false',
      spoolman_url: '',
      spoolman_sync_mode: 'off',
      spoolman_disable_weight_sync: 'false',
      spoolman_report_partial_usage: 'false',
    }),
    getSpoolmanInventorySpools: vi.fn().mockResolvedValue([]),
    getSpoolmanSlotAssignments: vi.fn().mockResolvedValue([]),
    createSpool: vi.fn().mockResolvedValue({ id: 4 }),
    createSpoolmanInventorySpool: vi.fn().mockResolvedValue({ id: 4 }),
  },
  spoolbuddyApi: {
    getDevices: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, fallback?: string) => fallback ?? key,
    i18n: { language: 'en', changeLanguage: vi.fn() },
  }),
}));

const TAG = 'AABB1122334455FF';

function renderWithUnknownTag() {
  const ctx = {
    selectedPrinterId: null,
    setSelectedPrinterId: vi.fn(),
    sbState: {
      weight: null,
      weightStable: false,
      rawAdc: null,
      matchedSpool: null,
      unknownTagUid: TAG,
      unknownTrayUuid: null,
      deviceOnline: true,
      deviceId: 'dev-1',
      remainingWeight: null,
      netWeight: null,
    },
    setAlert: vi.fn(),
    displayBrightness: 100,
    setDisplayBrightness: vi.fn(),
    displayBlankTimeout: 0,
    setDisplayBlankTimeout: vi.fn(),
  };
  function Wrapper() {
    return <Outlet context={ctx} />;
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <ToastProvider>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/spoolbuddy']}>
          <Routes>
            <Route element={<Wrapper />}>
              <Route path="spoolbuddy" element={<SpoolBuddyDashboard />} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </ToastProvider>,
  );
}

async function quickAdd() {
  fireEvent.click(await screen.findByRole('button', { name: 'Add to Inventory' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Add Anyway' }));
  await waitFor(() => expect(api.createSpool).toHaveBeenCalledTimes(1));
  return vi.mocked(api.createSpool).mock.calls[0][0];
}

describe('SpoolBuddy quick-add label weight', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("creates the roll at the setting's figure", async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ default_spool_weight_g: 1100 } as never);
    renderWithUnknownTag();
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());

    const payload = await quickAdd();
    expect(payload.label_weight).toBe(1100);
    expect(payload.tag_uid).toBe(TAG);
    expect(api.createSpoolmanInventorySpool).not.toHaveBeenCalled();
  });

  it('follows the setting, not a fixed figure', async () => {
    vi.mocked(api.getSettings).mockResolvedValue({ default_spool_weight_g: 850 } as never);
    renderWithUnknownTag();
    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());

    expect((await quickAdd()).label_weight).toBe(850);
  });

  it('sends no figure (0) while the setting is unresolved, for the backend to fill', async () => {
    vi.mocked(api.getSettings).mockReturnValue(new Promise(() => {}) as never);
    renderWithUnknownTag();

    expect((await quickAdd()).label_weight).toBe(0);
  });
});
