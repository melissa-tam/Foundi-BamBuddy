/**
 * LOCATION column presence qualifier (W5b).
 *
 * The assignments API returns a tri-state `present` per binding. When it is
 * verifiably `false` the slot reads cleared, so the bare "Printer X A1" badge
 * would tell the operator the roll is loaded in a printer when it is actually
 * on a shelf. The column now suffixes the badge with WHY, and — critically —
 * only ever does so for `present === false`: `true`, `null` and an absent field
 * (older backend, partial push, offline printer) all keep the historical badge,
 * because unknown presence is not evidence of absence.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { waitFor } from '@testing-library/react';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const mockSpool = {
  id: 42,
  material: 'PETG',
  subtype: 'HF',
  brand: 'Overture',
  color_name: 'Slate',
  rgba: '556677FF',
  label_weight: 1000,
  core_weight: 250,
  weight_used: 400,
  slicer_filament: null,
  slicer_filament_name: null,
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  added_full: null,
  last_used: null,
  encode_time: null,
  tag_uid: null,
  tray_uuid: null,
  data_origin: 'manual',
  tag_type: null,
  archived_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  k_profiles: [],
  cost_per_kg: null,
  last_scale_weight: null,
  last_weighed_at: null,
  storage_location: null,
  spent_at: null,
};

/** Bound to printer "Rocket" AMS A slot 1 — formatSlotLabel(0, 0) => "A1". */
const baseAssignment = {
  id: 7,
  spool_id: 42,
  printer_id: 3,
  printer_name: 'Rocket',
  ams_id: 0,
  tray_id: 0,
  ams_label: null,
  configured: true,
  created_at: '2026-01-01T00:00:00Z',
};

function mount(
  assignment: Record<string, unknown>,
  spool: Record<string, unknown> = mockSpool,
) {
  server.use(
    http.get('/api/v1/settings/spoolman', () =>
      HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' })
    ),
    http.get('/api/v1/inventory/spools', () => HttpResponse.json([spool])),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([assignment])),
    http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
  );
  return render(<InventoryPageRouter />);
}

describe('InventoryPage — LOCATION presence qualifier', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('suffixes "not inserted" when the slot reads empty and nothing explains it', async () => {
    const { container } = mount({ ...baseAssignment, present: false });

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).toContain('A1');
    expect(container.textContent).toContain('not inserted');
    // The stale-claim wording must not be confused with the other two states.
    expect(container.textContent).not.toContain('awaiting insert');
    expect(container.textContent).not.toContain('ran out');
  });

  it('suffixes "awaiting insert" when the binding was deliberately pre-configured', async () => {
    const { container } = mount({
      ...baseAssignment,
      present: false,
      pre_configured_at: '2026-08-01T10:00:00Z',
    });

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).toContain('awaiting insert');
  });

  it('suffixes "ran out" when the spool is spent — outranking pre_configured_at', async () => {
    // Both stamps present. `pre_configured_at` is a bind-time stamp that an
    // insert-then-run-dry cycle never clears, so "ran out" is the live truth
    // and "awaiting insert" is stale history.
    const { container } = mount(
      {
        ...baseAssignment,
        present: false,
        pre_configured_at: '2026-08-01T10:00:00Z',
      },
      { ...mockSpool, spent_at: '2026-08-01T18:00:00Z' },
    );

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).toContain('ran out');
    expect(container.textContent).not.toContain('awaiting insert');
  });

  it('renders the plain badge when the tray is present', async () => {
    const { container } = mount({ ...baseAssignment, present: true });

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).toContain('A1');
    expect(container.textContent).not.toContain('not inserted');
  });

  it('renders the plain badge when presence is unknown (null)', async () => {
    // Offline printer / partial push / a dialect that never reports presence.
    // Fail open — never accuse a binding of being stale on missing evidence.
    const { container } = mount({ ...baseAssignment, present: null });

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).not.toContain('not inserted');
  });

  it('renders the plain badge when the field is absent entirely (older backend)', async () => {
    const { container } = mount(baseAssignment);

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).not.toContain('not inserted');
  });

  it('does not qualify a spent spool that is still physically loaded', async () => {
    // A spool can be marked spent while the empty roll is still on the feeder.
    // Presence, not spent-ness, decides whether the location is a lie.
    const { container } = mount(
      { ...baseAssignment, present: true },
      { ...mockSpool, spent_at: '2026-08-01T18:00:00Z' },
    );

    await waitFor(() => {
      expect(container.textContent).toContain('Rocket');
    });
    expect(container.textContent).not.toContain('ran out — awaiting new roll');
  });
});
