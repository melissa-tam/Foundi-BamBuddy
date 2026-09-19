/**
 * Pager — the shared pagination bar (range summary, page size, first/prev/
 * next/last).
 *
 * Extracted from `ArchivesPage`'s local `ArchivePaginationBar` when the queue
 * history needed the same control; its copy moved with it, from
 * `archives.pagination.*` to `common.pagination.*`, because the strings were
 * never about archives.
 *
 * Accessibility notes (the original had neither): the four direction controls
 * are icon-only, so each carries an `aria-label`; the page-size `<select>` is
 * labelled by the visible "Show" caption through `aria-labelledby`.
 *
 * The four nav buttons are `aria-disabled`, never natively `disabled`. A
 * native `disabled` leaves the focus order the moment it is set, so activating
 * "Next" onto the LAST page disabled the very button under the keyboard user's
 * focus and dropped them to `document.body` — they lost their place in the
 * page (WCAG 2.4.3). `aria-disabled` keeps the control focusable and announced
 * as unavailable, and the handler is a no-op, which is the fix rather than an
 * effect that juggles focus somewhere else afterwards.
 *
 * `totalPages <= 1` renders nothing — except under "All", where the summary is
 * the only place the row count is stated.
 */
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import { useId } from 'react';

/** Sentinel page size meaning "no paging — show every row". */
export const PAGE_SIZE_ALL = -1;

const DEFAULT_PAGE_SIZES = [25, 50, 100, 200];

export interface PagerProps {
  /** Zero-based page. */
  pageIndex: number;
  /** Rows per page, or `PAGE_SIZE_ALL`. */
  pageSize: number;
  /** Rows across every page (after filtering). */
  totalRows: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  /** Plural noun for the counted rows, already translated ("prints"). */
  unitLabel: string;
  /** Page-size choices; `PAGE_SIZE_ALL` is appended as "All". */
  pageSizes?: number[];
  t: (key: string) => string;
}

export function Pager({
  pageIndex,
  pageSize,
  totalRows,
  totalPages,
  onPageChange,
  onPageSizeChange,
  unitLabel,
  pageSizes = DEFAULT_PAGE_SIZES,
  t,
}: PagerProps) {
  const sizeLabelId = useId();
  const isShowAll = pageSize === PAGE_SIZE_ALL;
  if (totalPages <= 1 && !isShowAll) return null;
  const effectiveSize = isShowAll ? totalRows || 1 : pageSize;
  const atStart = pageIndex === 0;
  const atEnd = pageIndex >= totalPages - 1;

  /** Unavailable is a STATE, not a removal: same look, still focusable. */
  const navClass = (unavailable: boolean) =>
    `p-1.5 rounded transition-colors ${
      unavailable
        ? 'text-bambu-gray opacity-30 cursor-not-allowed'
        : 'text-bambu-gray hover:text-white'
    }`;

  const goTo = (unavailable: boolean, page: number) => () => {
    if (unavailable) return;
    onPageChange(page);
  };
  return (
    <div className="flex items-center justify-between pt-2 text-sm">
      <span className="text-bambu-gray">
        {isShowAll
          ? `${totalRows} ${unitLabel}`
          : <>{t('common.pagination.showing')} {pageIndex * effectiveSize + 1} {t('common.pagination.to')}{' '}
              {Math.min((pageIndex + 1) * effectiveSize, totalRows)}{' '}
              {t('common.pagination.of')} {totalRows} {unitLabel}</>
        }
      </span>
      <div className="flex items-center gap-2">
        <span id={sizeLabelId} className="text-bambu-gray">{t('common.pagination.show')}</span>
        <select
          aria-labelledby={sizeLabelId}
          value={pageSize}
          onChange={(e) => onPageSizeChange(Number(e.target.value))}
          className="px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm focus:outline-none focus:border-bambu-green"
        >
          {pageSizes.map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
          <option value={PAGE_SIZE_ALL}>{t('common.pagination.all')}</option>
        </select>
        {!isShowAll && (
          <>
            <button
              type="button"
              aria-label={t('common.pagination.first')}
              aria-disabled={atStart}
              onClick={goTo(atStart, 0)}
              className={navClass(atStart)}
            >
              <ChevronsLeft className="w-4 h-4" />
            </button>
            <button
              type="button"
              aria-label={t('common.pagination.previous')}
              aria-disabled={atStart}
              onClick={goTo(atStart, Math.max(0, pageIndex - 1))}
              className={navClass(atStart)}
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-bambu-gray px-2 whitespace-nowrap">
              {t('common.pagination.page')} {pageIndex + 1} {t('common.pagination.of')} {totalPages}
            </span>
            <button
              type="button"
              aria-label={t('common.pagination.next')}
              aria-disabled={atEnd}
              onClick={goTo(atEnd, Math.min(totalPages - 1, pageIndex + 1))}
              className={navClass(atEnd)}
            >
              <ChevronRight className="w-4 h-4" />
            </button>
            <button
              type="button"
              aria-label={t('common.pagination.last')}
              aria-disabled={atEnd}
              onClick={goTo(atEnd, totalPages - 1)}
              className={navClass(atEnd)}
            >
              <ChevronsRight className="w-4 h-4" />
            </button>
          </>
        )}
      </div>
    </div>
  );
}
