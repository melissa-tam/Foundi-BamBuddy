/**
 * The Add spool form starts from the `default_spool_weight_g` setting
 * (`hooks/useDefaultSpoolWeight`), never a client literal; editing or copying a
 * row shows that row's own label.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import { render } from '../utils';
import { SpoolFormModal } from '../../components/SpoolFormModal';
import { api } from '../../api/client';
import type { InventorySpool } from '../../api/client';

vi.mock('../../api/client', () => ({
  api: {
    getSettings: vi.fn(),
    getAuthStatus: vi.fn().mockResolvedValue({ auth_enabled: false }),
    getCloudStatus: vi.fn().mockResolvedValue({ is_authenticated: false }),
    getFilamentPresets: vi.fn().mockResolvedValue([]),
    getSpoolCatalog: vi.fn().mockResolvedValue([]),
    getLocations: vi.fn().mockResolvedValue([]),
    getColorCatalog: vi.fn().mockResolvedValue([]),
    getLocalPresets: vi.fn().mockResolvedValue({ filament: [] }),
    getBuiltinFilaments: vi.fn().mockResolvedValue([]),
    getPrinters: vi.fn().mockResolvedValue([]),
    getSpools: vi.fn().mockResolvedValue([]),
    getSpoolUsageHistory: vi.fn().mockResolvedValue([]),
  },
}));

vi.mock('../../contexts/ToastContext', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../contexts/ToastContext')>();
  return { ...actual, useToast: () => ({ showToast: vi.fn() }) };
});

const row: InventorySpool = {
  id: 7,
  material: 'PLA',
  subtype: 'Basic',
  brand: 'Polymaker',
  color_name: 'Red',
  rgba: 'FF0000FF',
  extra_colors: null,
  effect_type: null,
  label_weight: 750,
  core_weight: 250,
  core_weight_catalog_id: null,
  weight_used: 100,
  slicer_filament: 'GFL99',
  slicer_filament_name: 'Generic PLA',
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  added_full: null,
  last_used: null,
  encode_time: null,
  tag_uid: null,
  tray_uuid: null,
  data_origin: null,
  tag_type: null,
  archived_at: null,
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
  k_profiles: [],
  cost_per_kg: null,
};

function labelWeightField(): HTMLInputElement {
  const caption = screen.getByText('Label Weight');
  return within(caption.parentElement as HTMLElement).getByRole('spinbutton') as HTMLInputElement;
}

describe('SpoolFormModal label weight default', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (api.getSettings as ReturnType<typeof vi.fn>).mockResolvedValue({ default_spool_weight_g: 1100 });
  });

  it('opens Add spool on the default-weight setting', async () => {
    render(<SpoolFormModal isOpen onClose={vi.fn()} mode="create" currencySymbol="$" />);

    await waitFor(() => expect(labelWeightField()).toHaveValue(1100));
  });

  it('follows the setting, not a fixed figure', async () => {
    (api.getSettings as ReturnType<typeof vi.fn>).mockResolvedValue({ default_spool_weight_g: 850 });
    render(<SpoolFormModal isOpen onClose={vi.fn()} mode="create" currencySymbol="$" />);

    await waitFor(() => expect(labelWeightField()).toHaveValue(850));
  });

  it("edits a row at the row's own label, never the setting", async () => {
    render(<SpoolFormModal isOpen onClose={vi.fn()} mode="edit" spool={row} currencySymbol="$" />);

    await waitFor(() => expect(api.getSettings).toHaveBeenCalled());
    await waitFor(() => expect(labelWeightField()).toHaveValue(750));
  });
});
