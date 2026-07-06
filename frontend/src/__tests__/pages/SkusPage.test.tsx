/**
 * Frontend tests for the SKU catalog page (farm production, Phase 2).
 *
 * Coverage:
 * - List renders SKU rows (code, name, part number, file count, stats).
 * - Empty state shows the "no SKUs yet" copy + create CTA.
 * - Error state shows the load-error message + retry button.
 * - Create dialog blocks submission when code/name are blank, and the
 *   "Suggest from file" flow prefills code/part/name from GET /skus/suggest
 *   before a valid submit POSTs and refreshes the list.
 * - Save commits a pending add-row file selection (links via POST
 *   /skus/{id}/files without a separate "Add file" click), and when that link
 *   fails after the SKU persisted the dialog stays open with the reason inline.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { SkusPage } from '../../pages/SkusPage';
import type { Sku } from '../../types/skus';

function sku(overrides: Partial<Sku> = {}): Sku {
  return {
    id: 1,
    code: 'WID-001',
    name: 'Widget',
    part_number: 'PN-42',
    notes: null,
    default_eject_profile_id: null,
    files: [
      {
        id: 11,
        sku_id: 1,
        library_file_id: 5,
        library_file_name: 'widget.gcode.3mf',
        plate_index: 1,
        units_per_plate: 4,
        nozzle_diameter: 0.4,
        filament_type: 'PETG',
        printer_model: 'H2S',
        max_z_height: 12.5,
      },
    ],
    created_at: '2026-07-01T10:00:00Z',
    updated_at: '2026-07-01T10:00:00Z',
    ...overrides,
  };
}

const emptyStats = http.get('*/api/v1/skus/:id/stats', () =>
  HttpResponse.json({
    units_completed: 128,
    units_failed: 4,
    plates_completed: 32,
    plates_failed: 1,
    success_rate: 0.97,
    median_cycle_seconds: 5400,
  }),
);

// Dialog queries: eject profiles + library files fire on open.
const emptyEjectProfiles = http.get('*/api/v1/eject-profiles', () => HttpResponse.json([]));
const emptyLibraryFiles = http.get('*/api/v1/library/files', () => HttpResponse.json([]));

// A single 3MF library file the add-row can link, plus its plate metadata.
const libraryFile = {
  id: 5,
  folder_id: null,
  is_external: false,
  filename: 'widget.gcode.3mf',
  file_type: '3mf',
  file_size: 1024,
  thumbnail_path: null,
  print_count: 0,
  duplicate_count: 0,
  created_by_id: null,
  created_by_username: null,
  created_at: '2026-07-01T10:00:00Z',
  print_name: null,
  print_time_seconds: null,
  filament_used_grams: null,
  sliced_for_model: null,
};

const platesResponse = {
  file_id: 5,
  filename: 'widget.gcode.3mf',
  plates: [{ index: 1, name: null, objects: [] }],
  is_multi_plate: false,
};

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

describe('SkusPage', () => {
  it('renders the list of SKUs with stats', async () => {
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([sku()])),
      emptyStats,
      emptyEjectProfiles,
      emptyLibraryFiles,
    );

    render(<SkusPage />);

    expect(await screen.findByText('WID-001')).toBeInTheDocument();
    expect(screen.getByText('Widget')).toBeInTheDocument();
    expect(screen.getByText('PN-42')).toBeInTheDocument();
    // Lifetime stats resolve asynchronously per row.
    expect(await screen.findByText('128')).toBeInTheDocument();
    expect(screen.getByText('97%')).toBeInTheDocument();
  });

  it('shows the empty state with a create CTA when there are no SKUs', async () => {
    server.use(http.get('*/api/v1/skus', () => HttpResponse.json([])));

    render(<SkusPage />);

    expect(await screen.findByText('No SKUs yet')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /new sku/i })).toBeInTheDocument();
  });

  it('shows the error state with a retry button when the list fails to load', async () => {
    server.use(http.get('*/api/v1/skus', () => new HttpResponse(null, { status: 500 })));

    render(<SkusPage />);

    expect(await screen.findByText(/could not load skus/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  it('blocks the create dialog submit when code and name are blank', async () => {
    let postCalled = false;
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      emptyLibraryFiles,
      http.post('*/api/v1/skus', () => {
        postCalled = true;
        return HttpResponse.json(sku(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));

    const dialog = await screen.findByRole('dialog');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    expect(await screen.findByText(/code is required/i)).toBeInTheDocument();
    expect(screen.getByText(/name is required/i)).toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
    expect(postCalled).toBe(false);
  });

  it('surfaces a backend 409 duplicate-code rejection inline in the still-open dialog', async () => {
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      emptyLibraryFiles,
      http.post('*/api/v1/skus', () =>
        HttpResponse.json({ detail: "SKU code 'WID-001' already exists" }, { status: 409 }),
      ),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    await user.type(screen.getByLabelText(/^code$/i), 'WID-001');
    await user.type(screen.getByLabelText(/^name$/i), 'Widget');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // The backend detail renders inline (role=alert) inside the open dialog —
    // not just a transient toast — so the failure never dead-ends.
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent("SKU code 'WID-001' already exists");
    expect(dialog).toBeInTheDocument();
    // Form is re-submittable (button re-enabled after the failed attempt).
    expect(screen.getByRole('button', { name: /^save$/i })).toBeEnabled();
  });

  it('creates a SKU via the suggest-from-file flow and refreshes the list', async () => {
    let created = false;
    server.use(
      http.get('*/api/v1/skus/suggest', ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get('library_file_id')).toBe('5');
        return HttpResponse.json({
          code: 'GEAR-9',
          part_number: 'PN-9',
          name: 'Gear Nine',
          matched_from: 'gear9.gcode.3mf',
        });
      }),
      http.get('*/api/v1/skus', () =>
        HttpResponse.json(created ? [sku({ id: 9, code: 'GEAR-9', name: 'Gear Nine', part_number: 'PN-9' })] : []),
      ),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () =>
        HttpResponse.json([
          {
            id: 5,
            folder_id: null,
            is_external: false,
            filename: 'gear9.gcode.3mf',
            file_type: '3mf',
            file_size: 1024,
            thumbnail_path: null,
            print_count: 0,
            duplicate_count: 0,
            created_by_id: null,
            created_by_username: null,
            created_at: '2026-07-01T10:00:00Z',
            print_name: null,
            print_time_seconds: null,
            filament_used_grams: null,
            sliced_for_model: null,
          },
        ]),
      ),
      http.post('*/api/v1/skus', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        expect(body).toMatchObject({ code: 'GEAR-9', name: 'Gear Nine', part_number: 'PN-9' });
        created = true;
        return HttpResponse.json(sku({ id: 9, code: 'GEAR-9', name: 'Gear Nine', part_number: 'PN-9' }), {
          status: 201,
        });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    await screen.findByRole('dialog');

    // Pick the library file and run the suggestion.
    await user.selectOptions(await screen.findByLabelText(/library file/i), '5');
    await user.click(screen.getByRole('button', { name: /suggest/i }));

    // Suggestion prefilled the code/name fields.
    await waitFor(() => expect(screen.getByLabelText(/^code$/i)).toHaveValue('GEAR-9'));
    expect(screen.getByLabelText(/^name$/i)).toHaveValue('Gear Nine');
    expect(screen.getByLabelText(/part number/i)).toHaveValue('PN-9');

    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // The row appears after the invalidated list re-fetches. The dialog stays
    // open on create (so files can be linked immediately) — assert via the
    // table cell contents instead of dialog dismissal.
    expect(await screen.findByRole('cell', { name: 'GEAR-9' })).toBeInTheDocument();
  });

  it('links a pending add-row file selection when Save is pressed (no separate Add-file click)', async () => {
    const noFileSku = sku({ files: [] });
    let linkBody: Record<string, unknown> | null = null;

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([noFileSku])),
      http.get('*/api/v1/skus/:id', () => HttpResponse.json(noFileSku)),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      http.put('*/api/v1/skus/:id', () => HttpResponse.json(noFileSku)),
      http.post('*/api/v1/skus/:id/files', async ({ request }) => {
        linkBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(sku(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    // Open the edit dialog for a SKU that has no files yet.
    await screen.findByText('WID-001');
    await user.click(screen.getByRole('button', { name: /edit wid-001/i }));
    const dialog = await screen.findByRole('dialog');

    // Choose a file + units in the add-row but DON'T click "Add file" — the
    // operator just clicks the dialog's primary Save action.
    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');
    const units = within(dialog).getByLabelText('Units per plate');
    await user.clear(units);
    await user.type(units, '6');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // Save committed the link through the same POST /skus/{id}/files call as
    // the "Add file" button, then closed on full success.
    await waitFor(() => expect(linkBody).not.toBeNull());
    expect(linkBody).toMatchObject({ library_file_id: 5, plate_index: 1, units_per_plate: 6 });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('snaps the plate selection to the first sliced plate when plate 1 has no G-code', async () => {
    // Partially-sliced multi-plate 3MF: the backend lists ONLY sliced plates,
    // and plate 1 is not among them. The picker must not submit the stale
    // plate_index=1 seed (regression: "Plate 1 has no G-code in the 3MF").
    const noFileSku = sku({ files: [] });
    let linkBody: Record<string, unknown> | null = null;

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([noFileSku])),
      http.get('*/api/v1/skus/:id', () => HttpResponse.json(noFileSku)),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () =>
        HttpResponse.json({
          file_id: 5,
          filename: 'widget.gcode.3mf',
          plates: [{ index: 3, name: null, objects: [] }],
          is_multi_plate: true,
        }),
      ),
      http.put('*/api/v1/skus/:id', () => HttpResponse.json(noFileSku)),
      http.post('*/api/v1/skus/:id/files', async ({ request }) => {
        linkBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(sku(), { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('WID-001');
    await user.click(screen.getByRole('button', { name: /edit wid-001/i }));
    const dialog = await screen.findByRole('dialog');

    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');
    // Wait for the plates query to resolve and the select to show plate #3.
    await waitFor(() =>
      expect(within(dialog).getByLabelText('Plate')).toHaveValue('3'),
    );
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(linkBody).not.toBeNull());
    expect(linkBody).toMatchObject({ library_file_id: 5, plate_index: 3, units_per_plate: 1 });
  });

  it('keeps the dialog open with an inline error when the SKU saves but the file link fails', async () => {
    const noFileSku = sku({ files: [] });
    let putCalled = false;

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([noFileSku])),
      http.get('*/api/v1/skus/:id', () => HttpResponse.json(noFileSku)),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      http.put('*/api/v1/skus/:id', () => {
        putCalled = true;
        return HttpResponse.json(noFileSku);
      }),
      // Unsliced plate → backend 400 with a reason, AFTER the SKU persisted.
      http.post('*/api/v1/skus/:id/files', () =>
        HttpResponse.json({ detail: 'Plate 1 is not sliced' }, { status: 400 }),
      ),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('WID-001');
    await user.click(screen.getByRole('button', { name: /edit wid-001/i }));
    const dialog = await screen.findByRole('dialog');

    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // SKU persisted (PUT fired) but the link failed: the dialog stays open and
    // the failure reason renders inline, so the operator never exits believing
    // the file was linked.
    await waitFor(() => expect(putCalled).toBe(true));
    const alert = await screen.findByText(/but linking the file failed/i);
    expect(alert).toHaveTextContent('Plate 1 is not sliced');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    // Re-submittable: Save is re-enabled after the failed link attempt.
    expect(screen.getByRole('button', { name: /^save$/i })).toBeEnabled();
  });
});
