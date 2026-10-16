/**
 * The touch view (`/touch`): every printer as a large tile, as many as fit the screen
 * with no page scroll (`utils/touchGrid`), paged by swipe or by the bottom bar's
 * Previous / Next and page dots. Printers needing attention lead (`utils/touchOrder`),
 * and the order holds still while the screen is in use (`hooks/useHeldOrder`).
 *
 * Server state is the Printers page's own: the `['printers']` list and the
 * `['printerStatus', id]` queries the websocket keeps warm.
 */
import { useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type MouseEvent } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { ChevronLeft, ChevronRight, Loader2, LogOut } from 'lucide-react';
import { api } from '../api/client';
import { TouchTile } from '../components/touch/TouchTile';
import { TOUCH_EXIT_STATE } from '../components/touch/touchExit';
import { useHeldOrder } from '../hooks/useHeldOrder';
import { asDateFormat } from '../utils/date';
import { FAN_SPEED_DEFAULTS, parsePresetTriple } from '../utils/temperatureFanPresets';
import { clampPage, fitToCount, pageCount, pageSlice, swipeDirection, touchGrid } from '../utils/touchGrid';
import { touchOrder, type TouchEntry } from '../utils/touchOrder';

/** The page this screen last showed (per browser; a convenience, never required). */
export const TOUCH_PAGE_KEY = 'touchViewPage';

function readStoredPage(): number {
  try {
    const raw = localStorage.getItem(TOUCH_PAGE_KEY);
    const page = raw === null ? 0 : Number.parseInt(raw, 10);
    return Number.isFinite(page) && page >= 0 ? page : 0;
  } catch {
    return 0;
  }
}

function storePage(page: number) {
  try {
    localStorage.setItem(TOUCH_PAGE_KEY, String(page));
  } catch {
    // Storage blocked (private window, kiosk policy): the page is simply not remembered.
  }
}

/**
 * Events from a dialog bubble here through React's portal tree; a swipe or an arrow key
 * inside a dialog (or a text field) is the dialog's, never a page turn.
 */
function ownedElsewhere(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  if (target.closest('[role="dialog"]')) return true;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName);
}

interface Size {
  width: number;
  height: number;
}

/** The grid area's content size, kept current by a ResizeObserver. */
function useElementSize(ref: React.RefObject<HTMLElement | null>): Size {
  const [size, setSize] = useState<Size>({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      const padX = parseFloat(style.paddingLeft || '0') + parseFloat(style.paddingRight || '0');
      const padY = parseFloat(style.paddingTop || '0') + parseFloat(style.paddingBottom || '0');
      setSize({ width: Math.max(0, rect.width - padX), height: Math.max(0, rect.height - padY) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

export function TouchPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const gridRef = useRef<HTMLElement>(null);
  const size = useElementSize(gridRef);
  const [storedPage, setStoredPage] = useState<number>(readStoredPage);
  const swipeStart = useRef<{ x: number; y: number } | null>(null);
  const swiped = useRef(false);

  const { data: printers, isPending, isError, error } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 5 * 60 * 1000,
  });
  const statusQueries = useQueries({
    queries: (printers ?? []).map((printer) => ({
      queryKey: ['printerStatus', printer.id],
      queryFn: () => api.getPrinterStatus(printer.id),
      refetchInterval: 30000, // Fallback polling, WebSocket handles real-time
    })),
  });

  const entries: TouchEntry[] = (printers ?? []).map((printer, index) => ({
    printer,
    status: statusQueries[index]?.data,
  }));
  const statusById = new Map(entries.map((entry) => [entry.printer.id, entry]));
  const { order, hold } = useHeldOrder(touchOrder(entries));

  const grid = fitToCount(touchGrid(size.width, size.height), order.length);
  const pages = pageCount(order.length, grid.perPage);
  const page = clampPage(storedPage, pages);
  const visible = pageSlice(order, page, grid.perPage);

  const timeFormat = settings?.time_format || 'system';
  const dateFormat = asDateFormat(settings?.date_format);
  const fanPresets = parsePresetTriple(settings?.fan_speed_presets, FAN_SPEED_DEFAULTS, 0, 100);
  const requirePlateClear = settings?.require_plate_clear === true;

  const goTo = (next: number) => {
    const target = clampPage(next, pages);
    setStoredPage(target);
    storePage(target);
  };

  const onPointerDown = (e: PointerEvent<HTMLElement>) => {
    hold();
    swiped.current = false;
    swipeStart.current = ownedElsewhere(e.target) ? null : { x: e.clientX, y: e.clientY };
  };
  const onPointerUp = (e: PointerEvent<HTMLElement>) => {
    const start = swipeStart.current;
    swipeStart.current = null;
    if (!start) return;
    const direction = swipeDirection(e.clientX - start.x, e.clientY - start.y);
    if (direction === null) return;
    swiped.current = true;
    goTo(direction === 'next' ? page + 1 : page - 1);
  };
  // A swipe that ends on a button is a page turn, never a press.
  const onClickCapture = (e: MouseEvent<HTMLElement>) => {
    if (!swiped.current) return;
    swiped.current = false;
    e.preventDefault();
    e.stopPropagation();
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    hold();
    if (ownedElsewhere(e.target)) return;
    if (e.key === 'ArrowRight') goTo(page + 1);
    else if (e.key === 'ArrowLeft') goTo(page - 1);
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col" onKeyDown={onKeyDown}>
      <main
        ref={gridRef}
        aria-label={t('touch.title')}
        className="min-h-0 flex-1 p-3 [touch-action:pan-y_pinch-zoom]"
        onPointerDown={onPointerDown}
        onPointerUp={onPointerUp}
        onPointerCancel={() => { swipeStart.current = null; }}
        onClickCapture={onClickCapture}
      >
        {isPending ? (
          <p className="flex h-full items-center justify-center gap-2 text-lg text-bambu-gray" role="status">
            <Loader2 className="h-6 w-6 animate-spin" aria-hidden="true" />
            {t('touch.loading')}
          </p>
        ) : isError ? (
          <p className="flex h-full items-center justify-center text-lg text-red-300" role="alert">
            {t('touch.loadFailed', { message: error instanceof Error ? error.message : '' })}
          </p>
        ) : order.length === 0 ? (
          <p className="flex h-full items-center justify-center text-lg text-bambu-gray">{t('touch.empty')}</p>
        ) : (
          <div
            className="grid h-full gap-3"
            style={{
              gridTemplateColumns: `repeat(${grid.cols}, minmax(0, 1fr))`,
              gridTemplateRows: `repeat(${grid.rows}, minmax(0, 1fr))`,
            }}
          >
            {visible.map((id) => {
              const entry = statusById.get(id);
              if (!entry) return null;
              return (
                <TouchTile
                  key={id}
                  printer={entry.printer}
                  status={entry.status}
                  requirePlateClear={requirePlateClear}
                  fanPresets={fanPresets}
                  dateFormat={dateFormat}
                  timeFormat={timeFormat}
                  onOpenCamera={(printerId) => navigate(`/touch/camera/${printerId}`)}
                />
              );
            })}
          </div>
        )}
      </main>

      <nav
        aria-label={t('touch.pages')}
        className="flex h-[64px] flex-shrink-0 items-center gap-3 border-t border-bambu-dark-tertiary bg-bambu-dark-secondary px-3"
      >
        <button
          type="button"
          onClick={() => navigate('/', { state: TOUCH_EXIT_STATE })}
          className="flex min-h-[56px] items-center gap-2 rounded-xl px-4 text-base font-semibold text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
        >
          <LogOut className="h-5 w-5" aria-hidden="true" />
          {t('touch.exit')}
        </button>
        <div className="flex min-w-0 flex-1 items-center justify-center gap-3">
          <button
            type="button"
            onClick={() => goTo(page - 1)}
            disabled={page === 0}
            aria-label={t('touch.prev')}
            className="flex min-h-[56px] min-w-[64px] items-center justify-center rounded-xl bg-bambu-dark text-white hover:bg-bambu-dark-tertiary disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
          >
            <ChevronLeft className="h-7 w-7" aria-hidden="true" />
          </button>
          <div className="flex items-center">
            {Array.from({ length: pages }, (_, index) => (
              <button
                key={index}
                type="button"
                onClick={() => goTo(index)}
                aria-label={t('touch.goToPage', { page: index + 1 })}
                aria-current={index === page ? 'page' : undefined}
                className="flex min-h-[56px] min-w-[40px] items-center justify-center rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
              >
                <span
                  aria-hidden="true"
                  className={`block h-3 w-3 rounded-full ${index === page ? 'bg-bambu-green' : 'bg-bambu-gray/40'}`}
                />
              </button>
            ))}
          </div>
          <span className="whitespace-nowrap text-sm text-bambu-gray tabular-nums" aria-live="polite">
            {t('touch.pageOf', { page: page + 1, pages })}
          </span>
          <button
            type="button"
            onClick={() => goTo(page + 1)}
            disabled={page >= pages - 1}
            aria-label={t('touch.next')}
            className="flex min-h-[56px] min-w-[64px] items-center justify-center rounded-xl bg-bambu-dark text-white hover:bg-bambu-dark-tertiary disabled:opacity-30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green"
          >
            <ChevronRight className="h-7 w-7" aria-hidden="true" />
          </button>
        </div>
      </nav>
    </div>
  );
}
