/**
 * Frontend tests for the SKU catalog page (farm production, Phase 2).
 *
 * Coverage:
 * - List renders SKU rows (code, name, part number, file count, stats).
 * - Empty state shows the "no SKUs yet" copy + create CTA.
 * - Error state shows the load-error message + retry button.
 * - Create dialog blocks submission when code/name are blank, and picking a
 *   file in the add-row auto-suggests code/part/name from GET /skus/suggest —
 *   filling only fields the operator hasn't typed, with a plain-language toast
 *   mapped from matched_from (never the raw enum token).
 * - A ?createFromFile=<id> deep link opens the create dialog with that file
 *   preseeded and auto-suggest fired, then strips the param from the URL.
 * - Save commits a pending add-row file selection (links via POST
 *   /skus/{id}/files without a separate "Add file" click), and when that link
 *   fails after the SKU persisted the dialog stays open with the reason inline.
 * - Single-pass create: the create dialog exposes the file/plate/units add-row,
 *   so Save can chain createSku→addSkuFile in one submit; an untouched add-row
 *   is a plain create; a link failure transitions the dialog into edit mode
 *   with the reason inline.
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

// A no-op suggest response: create-mode file picks auto-fire GET /skus/suggest,
// so tests that select a file (but aren't asserting the suggestion) stub it to
// return nothing rather than let the request fall through to the network.
const emptySuggest = http.get('*/api/v1/skus/suggest', () =>
  HttpResponse.json({ code: null, part_number: null, name: null, matched_from: null }),
);

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
  // The ?createFromFile deep-link test mutates the URL; reset it so a later
  // test never inherits a stale query string.
  window.history.pushState({}, '', '/');
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

  it('moves focus into the create dialog on open and closes it on Escape (shared Modal)', async () => {
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      emptyLibraryFiles,
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));

    // Modal moves focus into the panel on open (WCAG dialog focus management).
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    // Escape dismisses the dialog (not blocked because no save is in flight).
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
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

  it('auto-suggests from the picked file, filling untouched fields but never clobbering typed input', async () => {
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      http.get('*/api/v1/skus/suggest', ({ request }) => {
        const url = new URL(request.url);
        // The picked file id drives the suggestion (single picker, no separate
        // "Suggest" button).
        expect(url.searchParams.get('library_file_id')).toBe('5');
        return HttpResponse.json({
          code: 'GEAR-9',
          part_number: 'PN-9',
          name: 'Suggested Name',
          matched_from: 'object_name',
        });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    // The operator types their OWN name first — a manual edit must survive the
    // auto-suggestion that follows.
    await user.type(within(dialog).getByLabelText(/^name$/i), 'My Custom Name');

    // Pick the file in the add-row → auto-suggest fires for that file.
    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');

    // Empty (untouched) fields fill from the suggestion...
    await waitFor(() => expect(within(dialog).getByLabelText(/^code$/i)).toHaveValue('GEAR-9'));
    expect(within(dialog).getByLabelText(/part number/i)).toHaveValue('PN-9');
    // ...but the pre-typed name is NOT clobbered.
    expect(within(dialog).getByLabelText(/^name$/i)).toHaveValue('My Custom Name');
  });

  it('shows a plain-language toast for matched_from=object_name (never the raw token)', async () => {
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      http.get('*/api/v1/skus/suggest', () =>
        HttpResponse.json({ code: 'GEAR-9', part_number: null, name: null, matched_from: 'object_name' }),
      ),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');

    // Plain-language copy, mapped from the enum — the raw token never surfaces.
    expect(
      await screen.findByText('Details filled from the part names in the file'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/object_name/)).not.toBeInTheDocument();
  });

  it('opens the create dialog preseeded from ?createFromFile and clears the param', async () => {
    // Arrive as the post-upload "Create SKU from this file" toast would.
    window.history.pushState({}, '', '/skus?createFromFile=5');

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      http.get('*/api/v1/skus/suggest', ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get('library_file_id')).toBe('5');
        return HttpResponse.json({
          code: 'PRESEED-1',
          part_number: null,
          name: null,
          matched_from: 'object_name',
        });
      }),
    );

    render(<SkusPage />);

    // The dialog opens in create mode with the file preseeded in the add-row.
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Create SKU')).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByLabelText('File')).toHaveValue('5'));
    // Auto-suggest fired for the preseeded file.
    await waitFor(() => expect(within(dialog).getByLabelText(/^code$/i)).toHaveValue('PRESEED-1'));
    // The deep-link param is stripped (replace — no history spam).
    await waitFor(() => expect(window.location.search).toBe(''));
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

  it('renders the file/plate/units add-row and link-on-save hint in the create dialog', async () => {
    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    // The add-row picker (extracted SkuLinkAddRow) renders in create mode...
    expect(within(dialog).getByLabelText('File')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Plate')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Units per plate')).toBeInTheDocument();
    // ...under the "link on save" hint that replaces the old "save first" copy.
    expect(
      within(dialog).getByText(/will be linked when you create the sku/i),
    ).toBeInTheDocument();
    // No standalone "Add file" button in create mode — Save commits the link.
    expect(within(dialog).queryByRole('button', { name: /add file/i })).not.toBeInTheDocument();
  });

  it('links a buffered add-row file in one pass on create (createSku then addSkuFile)', async () => {
    let createBody: Record<string, unknown> | null = null;
    let linkBody: Record<string, unknown> | null = null;
    const created = sku({ id: 7, code: 'NEW-7', name: 'New Seven', part_number: null, files: [] });

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      emptySuggest,
      http.post('*/api/v1/skus', async ({ request }) => {
        createBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(created, { status: 201 });
      }),
      http.post('*/api/v1/skus/:id/files', async ({ request }) => {
        linkBody = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(created, { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    await user.type(within(dialog).getByLabelText(/^code$/i), 'NEW-7');
    await user.type(within(dialog).getByLabelText(/^name$/i), 'New Seven');
    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');
    const units = within(dialog).getByLabelText('Units per plate');
    await user.clear(units);
    await user.type(units, '3');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // The SKU is created first, then its link attaches through the SAME POST
    // /skus/{id}/files call as the "Add file" button — one submit, no reopen.
    await waitFor(() => expect(createBody).not.toBeNull());
    expect(createBody).toMatchObject({ code: 'NEW-7', name: 'New Seven' });
    await waitFor(() => expect(linkBody).not.toBeNull());
    expect(linkBody).toMatchObject({ library_file_id: 5, plate_index: 1, units_per_plate: 3 });
    // Full success (create + link) closes the dialog.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('creates a SKU with no addSkuFile call when the create add-row is untouched', async () => {
    let linkCalled = false;
    const created = sku({ id: 8, code: 'PLAIN-8', name: 'Plain Eight', files: [] });

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      http.get('*/api/v1/skus/:id', () => HttpResponse.json(created)),
      http.post('*/api/v1/skus', () => HttpResponse.json(created, { status: 201 })),
      http.post('*/api/v1/skus/:id/files', () => {
        linkCalled = true;
        return HttpResponse.json(created, { status: 201 });
      }),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    await user.type(within(dialog).getByLabelText(/^code$/i), 'PLAIN-8');
    await user.type(within(dialog).getByLabelText(/^name$/i), 'Plain Eight');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // Plain create: the dialog transitions to edit mode (Add-file button
    // appears) so files can be attached — and no file link was posted.
    expect(await screen.findByRole('button', { name: /add file/i })).toBeInTheDocument();
    expect(linkCalled).toBe(false);
  });

  it('keeps the create dialog open (in edit mode) with an inline error when the link fails', async () => {
    const created = sku({ id: 9, code: 'FAIL-9', name: 'Fail Nine', files: [] });
    let postCalled = false;

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([])),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
      emptySuggest,
      http.get('*/api/v1/skus/:id', () => HttpResponse.json(created)),
      http.post('*/api/v1/skus', () => {
        postCalled = true;
        return HttpResponse.json(created, { status: 201 });
      }),
      // Unsliced plate → backend 400 AFTER the SKU was created.
      http.post('*/api/v1/skus/:id/files', () =>
        HttpResponse.json({ detail: 'Plate 1 is not sliced' }, { status: 400 }),
      ),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('No SKUs yet');
    await user.click(screen.getByRole('button', { name: /new sku/i }));
    const dialog = await screen.findByRole('dialog');

    await user.type(within(dialog).getByLabelText(/^code$/i), 'FAIL-9');
    await user.type(within(dialog).getByLabelText(/^name$/i), 'Fail Nine');
    await user.selectOptions(await within(dialog).findByLabelText('File'), '5');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    // SKU created but the link failed: the dialog transitions to edit mode for
    // the retry and surfaces the reason inline, never exiting as if it linked.
    await waitFor(() => expect(postCalled).toBe(true));
    const alert = await screen.findByText(/but linking the file failed/i);
    expect(alert).toHaveTextContent('Plate 1 is not sliced');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('renders the links list and the extracted add-row together in edit mode', async () => {
    const withFile = sku(); // seeded with one linked file

    server.use(
      http.get('*/api/v1/skus', () => HttpResponse.json([withFile])),
      http.get('*/api/v1/skus/:id', () => HttpResponse.json(withFile)),
      emptyStats,
      emptyEjectProfiles,
      http.get('*/api/v1/library/files', () => HttpResponse.json([libraryFile])),
      http.get('*/api/v1/library/files/:id/plates', () => HttpResponse.json(platesResponse)),
    );

    const user = userEvent.setup();
    render(<SkusPage />);

    await screen.findByText('WID-001');
    await user.click(screen.getByRole('button', { name: /edit wid-001/i }));
    const dialog = await screen.findByRole('dialog');

    // Existing linked file is listed (its remove control is unique to the list).
    expect(
      await within(dialog).findByRole('button', { name: /remove widget\.gcode\.3mf/i }),
    ).toBeInTheDocument();
    // ...and the extracted add-row + its Add-file button still render.
    expect(within(dialog).getByLabelText('File')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /add file/i })).toBeInTheDocument();
  });
});
