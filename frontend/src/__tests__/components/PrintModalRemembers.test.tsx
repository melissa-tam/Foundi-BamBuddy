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
    it('carries printer, eject profile, options and AMS mapping forward', async () => {
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
      // Seeded manual mapping flows through the mapping pipeline to the API.
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
});
