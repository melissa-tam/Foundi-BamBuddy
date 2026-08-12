/**
 * Tests for the PrintModal "remember & derive instead of re-ask" behaviors:
 *  - 2a requeue prefill (prefillFrom carries the prior item forward)
 *  - 2b SKU-derived eject default + hint, and explicit-None winning over it
 *  - 2e every partial fan-out failure surfaced inline, modal stays open
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintModal } from '../../components/PrintModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { PrintQueueItem } from '../../api/client';

const EJECT_LABEL = 'Eject profile (auto part removal)';

const mockPrinters = [
  { id: 1, name: 'X1 Carbon', model: 'X1C', ip_address: '192.168.1.100', enabled: true, is_active: true },
  { id: 2, name: 'P1S', model: 'P1S', ip_address: '192.168.1.101', enabled: true, is_active: true },
];

const createMockQueueItem = (overrides: Partial<PrintQueueItem> = {}): PrintQueueItem => ({
  id: 1,
  printer_id: 1,
  archive_id: 1,
  position: 1,
  scheduled_time: null,
  require_previous_success: false,
  auto_off_after: false,
  gcode_injection: false,
  manual_start: false,
  ams_mapping: null,
  plate_id: null,
  bed_levelling: true,
  flow_cali: false,
  vibration_cali: true,
  layer_inspect: false,
  timelapse: false,
  use_ams: true,
  status: 'completed',
  started_at: null,
  completed_at: null,
  error_message: null,
  created_at: '2024-01-01T00:00:00Z',
  archive_name: 'Test Print',
  archive_thumbnail: null,
  printer_name: 'Test Printer',
  print_time_seconds: 3600,
  batch_id: null,
  batch_name: null,
  ...overrides,
});

const mockOnClose = vi.fn();
const mockOnSuccess = vi.fn();

// The global test setup stubs window.localStorage with no-op vi.fn()s. Back
// them with a real in-memory store so the 2c memory round-trips across reopens.
function installMemoryLocalStorage(): void {
  const store = new Map<string, string>();
  vi.mocked(window.localStorage.getItem).mockImplementation((k: string) => (store.has(k) ? store.get(k)! : null));
  vi.mocked(window.localStorage.setItem).mockImplementation((k: string, v: string) => { store.set(k, v); });
  vi.mocked(window.localStorage.removeItem).mockImplementation((k: string) => { store.delete(k); });
  vi.mocked(window.localStorage.clear).mockImplementation(() => { store.clear(); });
}

describe('PrintModal remembers & derives', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    installMemoryLocalStorage();
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json(mockPrinters)),
      http.get('/api/v1/archives/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json({ filaments: [] })),
      http.get('/api/v1/printers/:id/status', () =>
        HttpResponse.json({ connected: true, state: 'IDLE', ams: [], vt_tray: [], nozzles: [] })),
      http.post('/api/v1/queue/', () => HttpResponse.json({ id: 1, status: 'pending' })),
    );
  });

  describe('2a — requeue prefill', () => {
    // Under the 2026-08-12 contract a stored `ams_mapping` is an operator
    // INSTRUCTION (an explicit slot pin), never a cached auto-derivation. This
    // case still POSTs [254] for exactly that reason: the requeued item pinned
    // the external holder, the target printer's vt_tray IS configured, so the
    // seed lands in `manualMappings` and reads back as a manual selection. The
    // sibling suite below pins the other half — every mapping the operator did
    // NOT pin (including a seed that no longer resolves) is omitted.
    it('re-posts a seeded mapping that still resolves as an operator pin, alongside printer/eject/options', async () => {
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.get('/api/v1/eject-profiles', () => HttpResponse.json([{ id: 7, name: 'Sweep A' }])),
        http.get('/api/v1/archives/:id/filament-requirements', () =>
          HttpResponse.json({ filaments: [{ slot_id: 1, type: 'PLA', color: '#FFFFFF', used_grams: 5 }] })),
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            ams: [],
            vt_tray: [{ id: 254, tray_type: 'PLA', tray_color: 'FFFFFFFF' }],
            nozzles: [],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ id: 1, status: 'pending' });
        }),
      );

      const item = createMockQueueItem({
        printer_id: 2,
        eject_profile_id: 7,
        ams_mapping: [254],
        timelapse: true,
        manual_start: true,
      });

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          prefillFrom={item}
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      // Eject profile preselected from the requeued item.
      const ejectSelect = (await screen.findByLabelText(EJECT_LABEL)) as HTMLSelectElement;
      await waitFor(() => expect(ejectSelect.value).toBe('7'));

      // The requeued item's printer is preselected.
      expect(screen.getByText('1 printer selected')).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(capturedBody).not.toBeNull());
      expect(capturedBody?.printer_id).toBe(2);
      expect(capturedBody?.eject_profile_id).toBe(7);
      expect(capturedBody?.timelapse).toBe(true);
      expect(capturedBody?.manual_start).toBe(true);
      // The seeded pin resolves against this printer's live external tray, so
      // it is a live operator instruction and IS persisted.
      expect(capturedBody?.ams_mapping).toEqual([254]);
    });
  });

  describe('2b — SKU-derived eject default', () => {
    beforeEach(() => {
      server.use(
        http.get('/api/v1/library/files/:id', () =>
          HttpResponse.json({
            id: 5,
            filename: 'part.gcode.3mf',
            file_type: '3mf',
            folder_id: null,
            project_id: null,
            file_hash: null,
            file_size_bytes: 1024,
            thumbnail_path: null,
            created_at: '2024-01-01T00:00:00Z',
            updated_at: '2024-01-01T00:00:00Z',
          })),
        http.get('/api/v1/library/files/:id/plates', () => HttpResponse.json({ is_multi_plate: false, plates: [] })),
        http.get('/api/v1/library/files/:id/filament-requirements', () =>
          HttpResponse.json({ file_id: 5, filename: 'part.gcode.3mf', filaments: [] })),
        http.get('/api/v1/eject-profiles', () => HttpResponse.json([{ id: 7, name: 'Sweep A' }])),
        http.get('/api/v1/skus', () =>
          HttpResponse.json([
            {
              id: 1,
              code: 'SKU007',
              name: 'Widget',
              part_number: null,
              notes: null,
              default_eject_profile_id: 7,
              files: [
                {
                  id: 1,
                  sku_id: 1,
                  library_file_id: 5,
                  library_file_name: 'part',
                  plate_index: 1,
                  units_per_plate: 1,
                  nozzle_diameter: null,
                  filament_type: null,
                  printer_model: null,
                  max_z_height: null,
                },
              ],
              created_at: '2024-01-01T00:00:00Z',
              updated_at: '2024-01-01T00:00:00Z',
            },
          ])),
      );
    });

    it('preselects the SKU default eject profile and shows the hint', async () => {
      render(
        <PrintModal
          mode="create"
          libraryFileId={5}
          archiveName="Part"
          initialSelectedPrinterIds={[1]}
          onClose={mockOnClose}
        />,
      );

      const ejectSelect = (await screen.findByLabelText(EJECT_LABEL)) as HTMLSelectElement;
      await waitFor(() => expect(ejectSelect.value).toBe('7'));
      expect(screen.getByText('Default from SKU SKU007')).toBeInTheDocument();
    });

    it('writes an explicit None to memory so it wins over the SKU default next time', async () => {
      const user = userEvent.setup();
      const { unmount } = render(
        <PrintModal
          mode="create"
          libraryFileId={5}
          archiveName="Part"
          initialSelectedPrinterIds={[1]}
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      const ejectSelect = (await screen.findByLabelText(EJECT_LABEL)) as HTMLSelectElement;
      await waitFor(() => expect(ejectSelect.value).toBe('7'));

      // Operator overrides to None — hint disappears immediately.
      await user.selectOptions(ejectSelect, '');
      expect(ejectSelect.value).toBe('');
      expect(screen.queryByText('Default from SKU SKU007')).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: /^print$/i }));
      await waitFor(() => expect(mockOnSuccess).toHaveBeenCalled());
      unmount();

      // Reopen: remembered None outranks the SKU default, hint stays hidden.
      render(
        <PrintModal
          mode="create"
          libraryFileId={5}
          archiveName="Part"
          initialSelectedPrinterIds={[1]}
          onClose={mockOnClose}
        />,
      );
      const reopened = (await screen.findByLabelText(EJECT_LABEL)) as HTMLSelectElement;
      // Let the ['skus'] query resolve so a mistaken re-apply would have a chance.
      await waitFor(() => expect(screen.getByText('Part')).toBeInTheDocument());
      expect(reopened.value).toBe('');
      expect(screen.queryByText('Default from SKU SKU007')).not.toBeInTheDocument();
    });
  });

  describe('2e — partial fan-out failure', () => {
    it('keeps the modal open and lists every failing dispatch', async () => {
      server.use(
        http.post('/api/v1/queue/', async ({ request }) => {
          const body = (await request.json()) as { printer_id: number };
          if (body.printer_id === 1) {
            return HttpResponse.json({ detail: 'Printer unreachable' }, { status: 400 });
          }
          return HttpResponse.json({ id: 2, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      await waitFor(() => expect(screen.getByText('Select all')).toBeInTheDocument());
      await user.click(screen.getByText('Select all'));
      await waitFor(() => expect(screen.getByText('2 printers selected')).toBeInTheDocument());

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      // Inline alert with the count title and the failing printer's error line.
      await waitFor(() => expect(screen.getByText('1 of 2 dispatches failed:')).toBeInTheDocument());
      expect(screen.getByText(/X1 Carbon:/)).toBeInTheDocument();

      // Modal is not dismissed on partial failure.
      expect(mockOnClose).not.toHaveBeenCalled();
      expect(mockOnSuccess).not.toHaveBeenCalled();
    });
  });

  /**
   * `ams_mapping` is an operator INSTRUCTION, never a cached derivation
   * (2026-08-12 contract; root-caused from the 003-H2S incident, where an
   * auto-derived `[254]` — printer 1's legitimately-matched external spool —
   * was stamped onto nine printers that have no external holder and dispatched
   * hours later with `use_ams=False`).
   *
   * The dialog previews per printer and persists a mapping ONLY where the
   * operator explicitly pinned slots; everything else is omitted from the body
   * so the backend matcher decides against live tray state at dispatch.
   */
  describe('ams_mapping is an operator instruction, not a cached derivation', () => {
    const whitePlaReqs = {
      filaments: [{ slot_id: 1, type: 'PLA', color: '#FFFFFF', used_grams: 5 }],
    };

    it('omits ams_mapping when a seeded pin no longer resolves (external holder unconfigured)', async () => {
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json(whitePlaReqs)),
        // Same requeue as the 2a case, but this printer's external holder is
        // UNCONFIGURED: the vt_tray reports no material, so gtid 254 is not a
        // live candidate and the seed resolves to nothing. That is precisely
        // the 003-H2S hardware shape — and the field must not be sent.
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            ams: [],
            vt_tray: [{ id: 254, tray_type: '' }],
            nozzles: [],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ id: 1, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          prefillFrom={createMockQueueItem({ printer_id: 2, ams_mapping: [254] })}
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      expect(await screen.findByText('1 printer selected')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(capturedBody).not.toBeNull());
      expect(capturedBody?.printer_id).toBe(2);
      expect(capturedBody).not.toHaveProperty('ams_mapping');
    });

    it('omits ams_mapping for a pure auto-match — a derivation is a preview, not an instruction', async () => {
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json(whitePlaReqs)),
        // Configured external holder that auto-matches the requirement: the
        // exact derivation that produced the incident's stored [254].
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            ams: [],
            vt_tray: [{ id: 254, tray_type: 'PLA', tray_color: 'FFFFFFFF' }],
            nozzles: [],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ id: 1, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          initialSelectedPrinterIds={[1]}
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      // The preview DID derive the external holder — proving the omission below
      // is the contract at work, not an empty/unresolved panel.
      const slotSelect = (await screen.findByTitle('Auto-matched')) as HTMLSelectElement;
      expect(slotSelect.value).toBe('254');

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(capturedBody).not.toBeNull());
      expect(capturedBody?.printer_id).toBe(1);
      expect(capturedBody).not.toHaveProperty('ams_mapping');
    });

    it('posts ams_mapping when the operator explicitly picks a slot', async () => {
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json(whitePlaReqs)),
        // Both an AMS slot and the external holder match. Auto-match takes the
        // AMS slot (gtid 0); the operator overrides to the external holder.
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            ams: [{ id: 0, tray: [{ id: 0, tray_type: 'PLA', tray_color: 'FFFFFFFF' }] }],
            vt_tray: [{ id: 254, tray_type: 'PLA', tray_color: 'FFFFFFFF' }],
            nozzles: [],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ id: 1, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          initialSelectedPrinterIds={[1]}
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      const slotSelect = (await screen.findByTitle('Auto-matched')) as HTMLSelectElement;
      expect(slotSelect.value).toBe('0');

      await user.selectOptions(slotSelect, '254');
      expect(await screen.findByTitle('Manually selected')).toBeInTheDocument();
      // The external pick is called out in TEXT, not by colour alone (WCAG
      // 1.4.1): an external roll is not weight-tracked and the printer demands
      // filament at the holder.
      expect(screen.getByText('External')).toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(capturedBody).not.toBeNull());
      expect(capturedBody?.ams_mapping).toEqual([254]);
    });

    it('posts only the pinned slots, masking every auto-matched slot to -1', async () => {
      let capturedBody: Record<string, unknown> | null = null;
      server.use(
        http.get('/api/v1/archives/:id/filament-requirements', () =>
          HttpResponse.json({
            filaments: [
              { slot_id: 1, type: 'PLA', color: '#FFFFFF', used_grams: 5 },
              { slot_id: 2, type: 'PETG', color: '#FF0000', used_grams: 5 },
            ],
          })),
        // Two interchangeable PLA slots (gtid 0/1) + one PETG slot (gtid 2):
        // auto-match takes 0 and 2, and the operator overrides slot 1 to gtid 1.
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            ams: [{
              id: 0,
              tray: [
                { id: 0, tray_type: 'PLA', tray_color: 'FFFFFFFF' },
                { id: 1, tray_type: 'PLA', tray_color: 'FFFFFFFF' },
                { id: 2, tray_type: 'PETG', tray_color: 'FF0000FF' },
              ],
            }],
            vt_tray: [],
            nozzles: [],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          capturedBody = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json({ id: 1, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          initialSelectedPrinterIds={[1]}
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      const autoSelects = await screen.findAllByTitle('Auto-matched');
      expect(autoSelects).toHaveLength(2);
      expect((autoSelects[0] as HTMLSelectElement).value).toBe('0');
      expect((autoSelects[1] as HTMLSelectElement).value).toBe('2');

      // Pin slot 1 only; slot 2 keeps its auto-match.
      await user.selectOptions(autoSelects[0], '1');

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(capturedBody).not.toBeNull());
      // Slot 1 carries the instruction; slot 2 is -1 = "no pin, matcher decides
      // at dispatch" — its auto-match (gtid 2) is a preview and is NOT frozen.
      expect(capturedBody?.ams_mapping).toEqual([1, -1]);
    });

    it('multi-printer: an auto-configured override is a derivation — omitted, not stored', async () => {
      const bodies: Record<string, unknown>[] = [];
      server.use(
        http.get('/api/v1/eject-profiles', () => HttpResponse.json([])),
        http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json(whitePlaReqs)),
        // Two trays: a single-tray unit would classify as AMS-HT and change the
        // slot labels the assertions below key on.
        http.get('/api/v1/printers/:id/status', () =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            nozzles: [],
            vt_tray: [],
            ams: [{
              id: 0,
              tray: [
                { id: 0, tray_type: 'PLA', tray_color: 'FFFFFFFF' },
                { id: 1, tray_type: 'PETG', tray_color: 'FF0000FF' },
              ],
            }],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          bodies.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json({ id: bodies.length, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      await waitFor(() => expect(screen.getByText('Select all')).toBeInTheDocument());
      await user.click(screen.getByText('Select all'));
      await waitFor(() => expect(screen.getByText('2 printers selected')).toBeInTheDocument());

      // Opening the per-printer editor auto-configures it from live trays —
      // the same shape the `per_printer_mapping_expanded` setting produces on
      // every selected printer. The operator picked NOTHING, so nothing is
      // stored, even though the override is populated and on screen.
      const overrideBoxes = await screen.findAllByLabelText('Custom mapping');
      // Wait for both printers' status to land — auto-configure is a no-op
      // while a printer's trays are still unknown.
      await waitFor(() => expect(screen.getAllByText('(1/1 matched)')).toHaveLength(2));
      await user.click(overrideBoxes[1]);
      const derived = (await screen.findByRole('option', { name: /^A1:/ })).closest('select') as HTMLSelectElement;
      expect(derived.value).toBe('0');

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(bodies).toHaveLength(2));
      expect(bodies[0]).not.toHaveProperty('ams_mapping');
      expect(bodies[1]).not.toHaveProperty('ams_mapping');
    });

    it('multi-printer: only the printer whose mapping the operator hand-picked gets ams_mapping', async () => {
      const bodies: Record<string, unknown>[] = [];
      server.use(
        http.get('/api/v1/eject-profiles', () => HttpResponse.json([])),
        http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json(whitePlaReqs)),
        // Printer 2 carries a second matching slot so an explicit pick is a
        // real choice (gtid 1) distinct from what auto-match would derive (0).
        http.get('/api/v1/printers/:id/status', ({ params }) =>
          HttpResponse.json({
            connected: true,
            state: 'IDLE',
            nozzles: [],
            vt_tray: [],
            ams: [{
              id: 0,
              tray: Number(params.id) === 2
                ? [
                    { id: 0, tray_type: 'PLA', tray_color: 'FFFFFFFF' },
                    { id: 1, tray_type: 'PLA', tray_color: 'FFFFFFFF' },
                  ]
                : [{ id: 0, tray_type: 'PLA', tray_color: 'FFFFFFFF' }],
            }],
          })),
        http.post('/api/v1/queue/', async ({ request }) => {
          bodies.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json({ id: bodies.length, status: 'pending' });
        }),
      );

      const user = userEvent.setup();
      render(
        <PrintModal
          mode="create"
          archiveId={1}
          archiveName="Benchy"
          onClose={mockOnClose}
          onSuccess={mockOnSuccess}
        />,
      );

      await waitFor(() => expect(screen.getByText('Select all')).toBeInTheDocument());
      await user.click(screen.getByText('Select all'));
      await waitFor(() => expect(screen.getByText('2 printers selected')).toBeInTheDocument());

      // Opt printer 2 (second card) into a custom mapping and HAND-PICK slot
      // A2. The pick is what makes it an instruction: opening the editor only
      // auto-configures it (see the case above), and the pick clears that flag.
      const overrideBoxes = await screen.findAllByLabelText('Custom mapping');
      expect(overrideBoxes).toHaveLength(2);
      await user.click(overrideBoxes[1]);

      const a2Option = await screen.findByRole('option', { name: /^A2:/ });
      const inlineSelect = a2Option.closest('select') as HTMLSelectElement;
      await user.selectOptions(inlineSelect, '1');

      await user.click(screen.getByRole('button', { name: /^print$/i }));

      await waitFor(() => expect(bodies).toHaveLength(2));
      const forPrinter1 = bodies.find((b) => b.printer_id === 1)!;
      const forPrinter2 = bodies.find((b) => b.printer_id === 2)!;
      // Printer 1 was never pinned — no instruction, no field (its mapping is
      // computed at dispatch). Printer 2 carries the hand-picked mapping only.
      expect(forPrinter1).not.toHaveProperty('ams_mapping');
      expect(forPrinter2.ams_mapping).toEqual([1]);
    });
  });
});
