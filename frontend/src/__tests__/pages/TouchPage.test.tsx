/**
 * The touch view end to end in jsdom: tiles in attention order, paging (arrows, dots,
 * swipe) with the grid fitted to a measured 1280 x 720 screen, the remembered page and a
 * blocked localStorage, the contextual buttons per state, the camera still asked for on
 * attention tiles only, a shared prompt at touch size, and Exit back to the Printers page.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { TouchPage, TOUCH_PAGE_KEY } from '../../pages/TouchPage';

interface Fixture {
  id: number;
  name: string;
  status: Record<string, unknown>;
  printer?: Record<string, unknown>;
}

const IDLE = { connected: true, state: 'IDLE', hms_errors: [], awaiting_plate_clear: false };

/** Ten printers: four needing attention, six that do not (two pages of eight). */
const FIXTURES: Fixture[] = [
  { id: 1, name: '001-H2S', status: { ...IDLE, state: 'RUNNING', progress: 42, remaining_time: 30, layer_num: 12, total_layers: 200 } },
  { id: 2, name: '002-H2S', status: { ...IDLE, state: 'FINISH', awaiting_plate_clear: true } },
  { id: 3, name: '003-H2S', status: { ...IDLE, state: 'PAUSE', progress: 10 } },
  { id: 4, name: '004-H2S', status: IDLE, printer: { service_hold: { since: '2026-10-16T08:00:00', release_at: null } } },
  {
    id: 5,
    name: '005-H2S',
    status: {
      ...IDLE,
      state: 'PAUSE',
      hold_chip: { variant: 'incident', tone: 'held', label: { key: 'printers.incident.plate_vision', values: {} }, tooltip: [], qualifier: null },
      plate_check_exit: { print_error: '0500808C', job_id: '77', actions: ['IGNORE_RESUME'], deadline_at: null },
    },
  },
  { id: 6, name: '006-H2S', status: IDLE },
  { id: 7, name: '007-H2C', status: IDLE },
  { id: 8, name: '008-H2S', status: IDLE },
  { id: 9, name: '009-H2S', status: IDLE },
  { id: 10, name: '010-H2S', status: IDLE },
];

function printerRow(f: Fixture) {
  return {
    id: f.id,
    name: f.name,
    serial_number: `SN${f.id}`,
    ip_address: '127.0.0.1',
    model: 'H2S',
    location: null,
    nozzle_count: 1,
    is_active: true,
    auto_archive: true,
    external_camera_url: null,
    external_camera_type: null,
    external_camera_enabled: false,
    external_camera_snapshot_url: null,
    camera_rotation: 0,
    plate_detection_enabled: false,
    quarantined: false,
    quarantine_reason: null,
    service_hold: null,
    created_at: '',
    updated_at: '',
    ...f.printer,
  };
}

let stillRequests: number[] = [];

function serve(fixtures: Fixture[] = FIXTURES) {
  stillRequests = [];
  server.use(
    http.get('/api/v1/printers/', () => HttpResponse.json(fixtures.map(printerRow))),
    http.get('/api/v1/printers/:id/status', ({ params }) => {
      const f = fixtures.find((x) => x.id === Number(params.id));
      return HttpResponse.json({ id: Number(params.id), name: f?.name, ...f?.status });
    }),
    http.get('/api/v1/printers/:id/camera/still', ({ params }) => {
      stillRequests.push(Number(params.id));
      return new HttpResponse(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), {
        headers: { 'Content-Type': 'image/jpeg', ETag: `"${params.id}-1"`, 'X-Captured-At': '2026-10-16T10:00:00+00:00' },
      });
    }),
    http.get('/api/v1/settings/', () => HttpResponse.json({ time_format: '24h', require_plate_clear: true })),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
}

/** The tile headings, in screen order. */
const tileNames = () => screen.getAllByRole('article').map((a) => within(a).getByRole('heading').textContent);
const tile = (name: string) => screen.getByRole('article', { name });

describe('TouchPage', () => {
  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockReset();
    vi.mocked(localStorage.setItem).mockReset();
    window.history.pushState({}, '', '/touch');
    // The grid area of a 1280 x 720 screen: 4 x 2 tiles per page.
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      width: 1256, height: 632, top: 0, left: 0, right: 1256, bottom: 632, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect);
    serve();
  });

  afterEach(() => vi.restoreAllMocks());

  it('shows attention first, then the rest by name, eight to a page', async () => {
    render(<TouchPage />);
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(8));
    await waitFor(() =>
      expect(tileNames()).toEqual(['005-H2S', '002-H2S', '003-H2S', '004-H2S', '001-H2S', '006-H2S', '007-H2C', '008-H2S']),
    );
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();
  });

  it('pages with Next / Previous and the dots, and remembers the page', async () => {
    const user = userEvent.setup();
    render(<TouchPage />);
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(8));

    await user.click(screen.getByRole('button', { name: 'Next page' }));
    expect(screen.getByText('Page 2 of 2')).toBeInTheDocument();
    await waitFor(() => expect(tileNames()).toEqual(['009-H2S', '010-H2S']));
    expect(localStorage.setItem).toHaveBeenCalledWith(TOUCH_PAGE_KEY, '1');
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Page 1' }));
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
  });

  it('opens on the remembered page', async () => {
    vi.mocked(localStorage.getItem).mockImplementation((key) => (key === TOUCH_PAGE_KEY ? '1' : null));
    render(<TouchPage />);
    await waitFor(() => expect(screen.getByText('Page 2 of 2')).toBeInTheDocument());
  });

  it('works when localStorage throws', async () => {
    // Only the touch view's own key: the app's theme reads storage unguarded.
    vi.mocked(localStorage.getItem).mockImplementation((key) => {
      if (key === TOUCH_PAGE_KEY) throw new Error('SecurityError');
      return null;
    });
    vi.mocked(localStorage.setItem).mockImplementation((key) => {
      if (key === TOUCH_PAGE_KEY) throw new Error('SecurityError');
    });
    const user = userEvent.setup();
    render(<TouchPage />);
    await waitFor(() => expect(screen.getByText('Page 1 of 2')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: 'Next page' }));
    expect(screen.getByText('Page 2 of 2')).toBeInTheDocument();
  });

  it('pages on a swipe left, and a tap does not page', async () => {
    render(<TouchPage />);
    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(8));
    const grid = screen.getByRole('main');

    // jsdom has no PointerEvent: a MouseEvent of the pointer type carries the coordinates.
    const pointer = (type: string, clientX: number, clientY: number) =>
      fireEvent(grid, new MouseEvent(type, { bubbles: true, clientX, clientY }));
    pointer('pointerdown', 600, 300);
    pointer('pointerup', 590, 302);
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument();

    pointer('pointerdown', 600, 300);
    pointer('pointerup', 380, 310);
    expect(screen.getByText('Page 2 of 2')).toBeInTheDocument();
  });

  it('offers the contextual buttons per state', async () => {
    render(<TouchPage />);
    await waitFor(() => expect(tileNames()[0]).toBe('005-H2S'));

    const buttons = (name: string) =>
      within(tile(name))
        .getAllByRole('button')
        .map((b) => b.getAttribute('data-action'))
        .filter(Boolean);
    expect(buttons('005-H2S')).toEqual(['ignoreResume', 'stop']);
    expect(buttons('002-H2S')).toEqual(['markCleared']);
    expect(buttons('003-H2S')).toEqual(['resume']);
    expect(buttons('004-H2S')).toEqual(['exitMaintenance']);
    expect(buttons('001-H2S')).toEqual(['camera']);
    expect(buttons('006-H2S')).toEqual([]);
    for (const name of ['005-H2S', '002-H2S', '001-H2S', '006-H2S']) {
      expect(within(tile(name)).getByRole('button', { name: /More/ })).toBeInTheDocument();
    }
  });

  it('shows the camera still on attention tiles only', async () => {
    render(<TouchPage />);
    await waitFor(() => expect(tileNames()[0]).toBe('005-H2S'));

    for (const name of ['005-H2S', '002-H2S', '004-H2S']) {
      expect(within(tile(name)).getByRole('button', { name: `Live camera: ${name}` })).toBeInTheDocument();
    }
    for (const name of ['001-H2S', '006-H2S']) {
      expect(within(tile(name)).queryByRole('button', { name: `Live camera: ${name}` })).not.toBeInTheDocument();
    }
    await waitFor(() => expect(within(tile('002-H2S')).getByText(/^Picture /)).toBeInTheDocument());
    expect([...new Set(stillRequests)].sort((a, b) => a - b)).toEqual([2, 4, 5]);
  });

  it('opens the shared stop confirm at touch size from a tile', async () => {
    const user = userEvent.setup();
    render(<TouchPage />);
    await waitFor(() => expect(tileNames()[0]).toBe('005-H2S'));
    await user.click(within(tile('005-H2S')).getByRole('button', { name: 'Stop' }));
    const dialog = await screen.findByRole('dialog');
    const confirm = within(dialog).getAllByRole('button').at(-1);
    expect(confirm?.className).toContain('min-h-[64px]');
  });

  it('exits to the Printers page', async () => {
    const user = userEvent.setup();
    render(<TouchPage />);
    await user.click(await screen.findByRole('button', { name: 'Exit' }));
    expect(window.location.pathname).toBe('/');
  });

  it('says so when there are no printers', async () => {
    serve([]);
    render(<TouchPage />);
    expect(await screen.findByText('No printers configured')).toBeInTheDocument();
    expect(screen.getByText('Page 1 of 1')).toBeInTheDocument();
  });
});
