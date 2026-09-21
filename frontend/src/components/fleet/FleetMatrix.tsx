/**
 * The fleet matrix — printers × buckets, under one of three lenses.
 *
 * This is the "which printer is my problem" surface: one row per printer, one
 * column per bucket, the frozen Printer · Total · Avg block beside the name,
 * and the whole thing sorted worst-first on whichever lens is active. A cell
 * opens the bucket detail.
 *
 * PRESENTATIONAL over loaded data — the tab owns `/status` and `/overview` and
 * their loading, error and empty states. Everything here is derived from the
 * two payloads on every render; nothing is copied into state.
 *
 * ## Why a real `<table role="grid">`
 *
 * A 12 × 366 grid of divs is unreadable to assistive tech: there is no row
 * header, no column header, no "row 4 of 13". A real table gives the header
 * association for free — which is exactly why NO cell carries an
 * `aria-label`, since that attribute would replace the cell's content and
 * suppress the association it came for.
 *
 * ## The keyboard model (WAI-ARIA grid)
 *
 * The grid is ONE tab stop. Exactly one cell carries `tabIndex=0`; arrows move
 * it, Home/End reach the row's ends, Ctrl+Home/Ctrl+End the grid's corners,
 * PageUp/PageDown ±10 columns, and Enter or Space opens the focused bucket's
 * detail. Escape closes the detail and `Modal` restores focus to the cell that
 * opened it, so a keyboard drill-down returns exactly where it started.
 *
 * A cell that CONTAINS a single widget hands that widget the focus (the APG's
 * rule): the three sort buttons and the per-row Details control are reached by
 * arrowing to their cells, not by a second tab stop. That is why the key
 * handler ignores Enter and Space when the event came from a button — the
 * widget's own activation wins.
 *
 * `focusCell` commits the roving index only AFTER the browser confirms the
 * focus landed. On a phone the bucket columns are `display:none` until "Show
 * days" is pressed, and a hidden element cannot take focus; committing
 * regardless would move the grid's only tab stop onto an unreachable cell.
 *
 * ## Phone (~390 px)
 *
 * By `@container` query on this component's own root, never a viewport
 * breakpoint: the page width at one viewport varies with the persisted sidebar
 * toggle, so a viewport breakpoint would fire at the wrong moment half the
 * time. Narrow shows Printer · Total · Avg · one Time-split bar per row and
 * discloses the bucket columns behind "Show days" — ONE `<table>` and a
 * responsive column set. Nothing is dropped, only disclosed.
 */
import { useRef, useState, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useTheme } from '../../contexts/ThemeContext';
import { useTabs, type TabDefinition } from '../../hooks/useTabs';
import { InfoHint } from '../ui/InfoHint';
import { TabList, TabPanel } from '../ui/Tabs';
import { BucketDetail } from './BucketDetail';
import { FleetMatrixCell, MATRIX_CELL_BASE } from './FleetMatrixCell';
import { FleetMatrixLegend } from './FleetMatrixLegend';
import type {
  FleetOverview,
  FleetStatus,
  MatrixCell,
  MatrixPrinter,
  MatrixValues,
  SeriesBucket,
} from '../../types/fleetMetrics';
import {
  BUCKET_COLUMN_WIDTH_PX,
  FLEET_LENSES,
  LENS_LABEL_KEY,
  SECONDARY_TEXT_CLASS,
  bucketLabel,
  foldTimeSplit,
  formatCount,
  formatHours,
  formatInstantSiteDay,
  formatPercent,
  formatPrinters,
  formatSiteDate,
  isRowHidden,
  sumMap,
  type AbsenceHeader,
  type BucketLabel,
  type FleetLens,
} from '../../utils/fleetMetrics';

const SECONDS_PER_HOUR = 3600;

/** PageUp / PageDown move ten columns — a third of a month at day width. */
const PAGE_COLUMNS = 10;

/** Column indices of the frozen block, then the phone-only Time-split column. */
const COL_PRINTER = 0;
const COL_TOTAL = 1;
const COL_AVG = 2;
const COL_SPLIT = 3;
const FIRST_BUCKET_COL = 4;

/** Width of the phone-only Time-split column, in px. */
const SPLIT_COLUMN_WIDTH_PX = 72;

/** The opaque ground the frozen block scrolls under. Theme variable, all 8 grounds. */
const FROZEN_BG = 'bg-bambu-dark-secondary';

const FROZEN_CLASS: Record<number, string> = {
  [COL_PRINTER]: `sticky left-0 z-20 ${FROZEN_BG} w-28 max-w-28 @2xl:w-40 @2xl:max-w-40 text-left`,
  [COL_TOTAL]: `sticky left-28 @2xl:left-40 z-20 ${FROZEN_BG} w-14 max-w-14 @2xl:w-16 @2xl:max-w-16 text-right`,
  [COL_AVG]: `sticky left-[10.5rem] @2xl:left-56 z-20 ${FROZEN_BG} w-14 max-w-14 @2xl:w-16 @2xl:max-w-16 text-right`,
};

type SortKey = 'printer' | 'total' | 'avg';
type SortDirection = 'asc' | 'desc';

interface SortState {
  key: SortKey;
  direction: SortDirection;
}

const SORT_COLUMN_LABEL_KEY: Record<SortKey, string> = {
  printer: 'fleetMetrics.matrix.columns.printer',
  total: 'fleetMetrics.matrix.columns.total',
  avg: 'fleetMetrics.matrix.columns.average',
};

/**
 * "Worst first", stated per lens because worst is a different direction in
 * each: FEWEST prints, MOST hours down, LOWEST printing share. The Time-split
 * lens's Avg column carries hours down per day (a share has no per-day twin in
 * the payload), so its worst is the most, not the least.
 */
function worstFirst(lens: FleetLens, key: SortKey): SortDirection {
  if (key === 'printer') return 'asc';
  if (lens === 'prints') return 'asc';
  if (lens === 'hours_down') return 'desc';
  return key === 'total' ? 'asc' : 'desc';
}

const ARIA_SORT: Record<SortDirection, 'ascending' | 'descending'> = {
  asc: 'ascending',
  desc: 'descending',
};

/** One row's two frozen figures under the active lens. */
interface RowFigures {
  total: number | null;
  avg: number | null;
}

/**
 * Total and Avg come from the PAYLOAD — the window's totals cell and the
 * per-printer per-day rates the backend already divided by each printer's own
 * counted days. The component never re-derives a rate from seconds it summed
 * itself; the one sum it does make is the fleet row's Avg, which is the
 * fleet-wide per-day figure and is by definition the printers' rates added up.
 */
function rowFigures(
  lens: FleetLens,
  cell: MatrixCell | undefined,
  perDay: number | null,
): RowFigures {
  if (!cell) return { total: null, avg: perDay };
  if (lens === 'prints') return { total: sumMap(cell.prints), avg: perDay };
  if (lens === 'hours_down') return { total: cell.down_seconds / SECONDS_PER_HOUR, avg: perDay };
  const split = foldTimeSplit(cell.class_seconds);
  return { total: split.total > 0 ? split.printing / split.total : null, avg: perDay };
}

export interface FleetMatrixProps {
  /** One window's history. The tab owns the query; this renders what arrived. */
  overview: FleetOverview;
  /**
   * The live tile, when it has answered. Used for ONE thing: `recording_since`,
   * which dates the "needs recorded printer state" reason on the disabled
   * Time-split lens. The matrix never waits on it.
   */
  status?: FleetStatus;
}

export function FleetMatrix({ overview, status }: FleetMatrixProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const { resolvedMode } = useTheme();

  const [lens, setLens] = useState<FleetLens>('prints');
  const [sort, setSort] = useState<SortState | null>(null);
  const [showDays, setShowDays] = useState(false);
  const [detail, setDetail] = useState<{ printerId: number; bucketIndex: number } | null>(null);
  const [active, setActive] = useState({ row: 0, col: 0 });

  const cells = useRef(new Map<string, HTMLElement>());

  const bucketWidth = overview.bucket;
  const columnWidth = BUCKET_COLUMN_WIDTH_PX[bucketWidth];
  /** The hour lens is 14 px wide: too small to be a pointer target (2.5.8). */
  const rowControl = bucketWidth === 'hour';

  const buckets: SeriesBucket<MatrixValues>[] = overview.matrix.series.buckets;
  const totals = overview.matrix.series.totals;
  const labels: BucketLabel[] = buckets.map((bucket, index) =>
    bucketLabel(bucket, { bucket: bucketWidth, locale }, index === 0 ? undefined : buckets[index - 1]),
  );

  /** The window as one header, for the frozen block's own absence verdict. */
  const windowHeader: AbsenceHeader = {
    elapsed_seconds: buckets.reduce((sum, bucket) => sum + bucket.elapsed_seconds, 0),
    observed_seconds: buckets.reduce((sum, bucket) => sum + bucket.observed_seconds, 0),
  };

  const cellOf = (bucketIndex: number, printerId: number): MatrixCell | undefined =>
    buckets[bucketIndex]?.values.printers[String(printerId)];

  // Rows a deleted printer left behind with nothing to say about the window are
  // omitted; one that WAS in the fleet for part of it keeps its row.
  const visiblePrinters = overview.matrix.printers.filter(
    (printer) =>
      !isRowHidden(
        printer,
        buckets.map((_, index) => cellOf(index, printer.printer_id)),
      ),
  );

  /** A deleted printer's name is gone; the history about it is not. */
  const nameOf = (printer: MatrixPrinter): string =>
    printer.deleted
      ? t('fleetMetrics.matrix.deletedPrinter', { id: printer.printer_id })
      : printer.name;

  const figuresOf = (printer: MatrixPrinter): RowFigures =>
    rowFigures(
      lens,
      totals.printers[String(printer.printer_id)],
      lens === 'prints' ? printer.prints_per_day : printer.hours_down_per_day,
    );

  const effectiveSort: SortState = sort ?? { key: 'total', direction: worstFirst(lens, 'total') };

  const sorted = [...visiblePrinters].sort((left, right) => {
    if (effectiveSort.key === 'printer') {
      const byName = nameOf(left).localeCompare(nameOf(right), locale);
      return (effectiveSort.direction === 'asc' ? byName : -byName) || left.printer_id - right.printer_id;
    }
    const a = figuresOf(left)[effectiveSort.key];
    const b = figuresOf(right)[effectiveSort.key];
    return compareFigures(a, b, effectiveSort.direction) || left.printer_id - right.printer_id;
  });

  /** The Prints ramp is relative to the window's busiest PRINTER cell. */
  const printsMax = buckets.reduce((max, bucket) => {
    for (const printer of visiblePrinters) {
      const cell = bucket.values.printers[String(printer.printer_id)];
      if (cell) max = Math.max(max, sumMap(cell.prints));
    }
    return max;
  }, 0);

  const observedAny = buckets.some((bucket) => bucket.observed_seconds > 0);
  const timeSplitDisabled = !observedAny;
  const activeLens: FleetLens = timeSplitDisabled && lens === 'time_split' ? 'prints' : lens;

  const rowCount = 1 + sorted.length + 1;
  const columnCount = FIRST_BUCKET_COL + buckets.length;
  const activeRow = Math.min(active.row, rowCount - 1);
  const activeCol = Math.min(active.col, columnCount - 1);

  const register = (row: number, col: number) => (element: HTMLElement | null) => {
    const key = `${row}:${col}`;
    if (element) cells.current.set(key, element);
    else cells.current.delete(key);
  };

  const focusCell = (row: number, col: number) => {
    const element = cells.current.get(`${row}:${col}`);
    if (!element) return;
    element.focus();
    // A column hidden by the phone disclosure cannot take focus. Refusing the
    // move keeps the grid's single tab stop on a cell that still exists.
    if (document.activeElement !== element) return;
    setActive({ row, col });
    element.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  };

  const openDetail = (printerId: number, bucketIndex: number) => {
    if (bucketIndex < 0 || bucketIndex >= buckets.length) return;
    setDetail({ printerId, bucketIndex });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const lastRow = rowCount - 1;
    const lastCol = columnCount - 1;
    let next: { row: number; col: number } | null = null;

    switch (event.key) {
      case 'ArrowRight':
        next = { row: activeRow, col: Math.min(lastCol, activeCol + 1) };
        break;
      case 'ArrowLeft':
        next = { row: activeRow, col: Math.max(0, activeCol - 1) };
        break;
      case 'ArrowDown':
        next = { row: Math.min(lastRow, activeRow + 1), col: activeCol };
        break;
      case 'ArrowUp':
        next = { row: Math.max(0, activeRow - 1), col: activeCol };
        break;
      case 'Home':
        next = event.ctrlKey || event.metaKey ? { row: 0, col: 0 } : { row: activeRow, col: 0 };
        break;
      case 'End':
        next =
          event.ctrlKey || event.metaKey
            ? { row: lastRow, col: lastCol }
            : { row: activeRow, col: lastCol };
        break;
      case 'PageDown':
        next = { row: activeRow, col: Math.min(lastCol, activeCol + PAGE_COLUMNS) };
        break;
      case 'PageUp':
        next = { row: activeRow, col: Math.max(0, activeCol - PAGE_COLUMNS) };
        break;
      case 'Enter':
      case ' ': {
        // A cell holding a widget lets the widget answer for itself.
        if (event.target instanceof Element && event.target.closest('button')) return;
        const printer = activeRow >= 1 ? sorted[activeRow - 1] : undefined;
        if (printer && activeCol >= FIRST_BUCKET_COL) {
          openDetail(printer.printer_id, activeCol - FIRST_BUCKET_COL);
          event.preventDefault();
        }
        return;
      }
      default:
        return;
    }
    event.preventDefault();
    focusCell(next.row, next.col);
  };

  const cellPlumbing = (row: number, col: number) => ({
    tabIndex: row === activeRow && col === activeCol ? 0 : -1,
    onFocus: () => setActive({ row, col }),
  });

  const bucketClass = (extra = '') =>
    `${showDays ? '' : 'hidden @2xl:table-cell'} ${extra}`.trim();

  const lensItems: TabDefinition<FleetLens>[] = FLEET_LENSES.map((item) => ({
    id: item,
    label: t(LENS_LABEL_KEY[item]),
    disabled: item === 'time_split' && timeSplitDisabled,
  }));
  const tabs = useTabs<FleetLens>({ value: activeLens, onChange: setLens, items: lensItems });

  /**
   * Why Time split is unavailable — two different reasons, never conflated.
   *
   * The lens needs recorded printer state, and there are two ways not to have
   * it: nothing has EVER been recorded (`recording_since` is null), or
   * recording started after this window ended. Only the second has a date, and
   * the date is the recorder's FIRST instant read in the SITE's zone — the old
   * fallback here named the window's own end date, which is not when recording
   * started and told the reader something untrue.
   *
   * `status` is optional on this component but is in practice always present:
   * the history query does not run until `/status` has answered with the site's
   * today. Absent, the weaker claim is the honest one.
   */
  const timeSplitReason =
    status?.recording_since == null
      ? t('fleetMetrics.matrix.timeSplitDisabledNoData')
      : t('fleetMetrics.matrix.timeSplitDisabled', {
          date: formatInstantSiteDay(status.recording_since, overview.tz_name, locale),
        });

  const detailPrinter = detail
    ? overview.matrix.printers.find((printer) => printer.printer_id === detail.printerId)
    : undefined;
  const detailBucket = detail ? buckets[detail.bucketIndex] : undefined;

  return (
    <div className="@container space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <TabList tabs={tabs} ariaLabel={t('fleetMetrics.matrix.lensAriaLabel')} variant="pill" />
        {timeSplitDisabled ? (
          // The reason rides an adjacent InfoHint rather than the tab itself: a
          // focusable tooltip trigger nested inside a `role="tab"` button is
          // invalid, and `TabDefinition` carries no description slot.
          <InfoHint text={timeSplitReason} />
        ) : null}
        <button
          type="button"
          aria-expanded={showDays}
          onClick={() => setShowDays((shown) => !shown)}
          className={`@2xl:hidden ml-auto rounded-md border border-bambu-dark-tertiary px-2 py-1 text-xs ${SECONDARY_TEXT_CLASS} hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50`}
        >
          {showDays ? t('fleetMetrics.matrix.hideDays') : t('fleetMetrics.matrix.showDays')}
        </button>
      </div>

      <TabPanel tabs={tabs} className="space-y-2">
        <FleetMatrixLegend
          lens={activeLens}
          bucketWidth={bucketWidth}
          mode={resolvedMode}
          printsMax={printsMax}
        />

        <div className="overflow-x-auto">
          <table role="grid" className="border-separate border-spacing-0 text-xs">
            <caption className="sr-only">
              {t('fleetMetrics.matrix.caption', {
                lens: t(LENS_LABEL_KEY[activeLens]),
                from: formatSiteDate(overview.date_from, locale),
                to: formatSiteDate(overview.date_to, locale),
              })}
            </caption>
            <thead>
              <tr>
                {(['printer', 'total', 'avg'] as const).map((key, index) => {
                  const isSorted = effectiveSort.key === key;
                  return (
                    <th
                      key={key}
                      scope="col"
                      aria-sort={isSorted ? ARIA_SORT[effectiveSort.direction] : 'none'}
                      className={`${FROZEN_CLASS[index]} border-b border-bambu-dark-tertiary px-1 py-1 align-bottom font-medium`}
                    >
                      <button
                        type="button"
                        ref={register(0, index)}
                        {...cellPlumbing(0, index)}
                        onKeyDown={handleKeyDown}
                        onClick={() =>
                          setSort(
                            isSorted
                              ? { key, direction: effectiveSort.direction === 'asc' ? 'desc' : 'asc' }
                              : { key, direction: worstFirst(activeLens, key) },
                          )
                        }
                        aria-label={t('fleetMetrics.matrix.sortBy', {
                          column: t(SORT_COLUMN_LABEL_KEY[key]),
                        })}
                        className={`w-full ${index === 0 ? 'text-left' : 'text-right'} focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset`}
                      >
                        {t(SORT_COLUMN_LABEL_KEY[key])}
                        <span aria-hidden="true">{isSorted ? (effectiveSort.direction === 'asc' ? ' ↑' : ' ↓') : ' ⇅'}</span>
                      </button>
                    </th>
                  );
                })}
                <th
                  scope="col"
                  ref={register(0, COL_SPLIT)}
                  {...cellPlumbing(0, COL_SPLIT)}
                  onKeyDown={handleKeyDown}
                  style={{ width: SPLIT_COLUMN_WIDTH_PX }}
                  className={`@2xl:hidden border-b border-bambu-dark-tertiary px-1 py-1 align-bottom font-medium ${SECONDARY_TEXT_CLASS}`}
                >
                  {t('fleetMetrics.matrix.lens.timeSplit')}
                </th>
                {buckets.map((bucket, index) => {
                  const label = labels[index];
                  if (!label) return null;
                  const newDay = index === 0 || labels[index - 1]?.date !== label.date;
                  return (
                    <th
                      key={bucket.start}
                      scope="col"
                      ref={register(0, FIRST_BUCKET_COL + index)}
                      {...cellPlumbing(0, FIRST_BUCKET_COL + index)}
                      onKeyDown={handleKeyDown}
                      style={{ width: columnWidth, minWidth: columnWidth, maxWidth: columnWidth }}
                      className={bucketClass(
                        `relative border-b border-bambu-dark-tertiary px-0 pt-4 pb-1 align-bottom font-normal ${SECONDARY_TEXT_CLASS} focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset`,
                      )}
                    >
                      {label.isCurrent || label.month ? (
                        <span
                          aria-hidden="true"
                          className="pointer-events-none absolute left-0 top-0 whitespace-nowrap text-[10px]"
                        >
                          {label.isCurrent ? t('fleetMetrics.units.today') : label.month}
                        </span>
                      ) : null}
                      <span
                        aria-hidden="true"
                        className={`block leading-tight ${label.isWeekend ? 'opacity-50' : ''}`}
                      >
                        {topLine(bucketWidth, label)}
                      </span>
                      <span aria-hidden="true" className="block leading-tight">
                        {bottomLine(bucketWidth, label, newDay)}
                      </span>
                      <span className="sr-only">
                        {bucketWidth === 'week'
                          ? t('fleetMetrics.units.weekOf', { date: label.full })
                          : label.full}
                        {label.isCurrent ? ` ${t('fleetMetrics.units.today')}` : ''}
                      </span>
                    </th>
                  );
                })}
              </tr>
            </thead>

            <tbody>
              {sorted.map((printer, rowIndex) => {
                const row = rowIndex + 1;
                const figures = figuresOf(printer);
                const totalsCell = totals.printers[String(printer.printer_id)];
                return (
                  <tr key={printer.printer_id}>
                    <th
                      scope="row"
                      ref={rowControl ? undefined : register(row, COL_PRINTER)}
                      {...(rowControl ? {} : cellPlumbing(row, COL_PRINTER))}
                      onKeyDown={handleKeyDown}
                      className={`${FROZEN_CLASS[COL_PRINTER]} border-b border-bambu-dark-tertiary px-1 py-1 font-normal focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset`}
                    >
                      <span className="block truncate">{nameOf(printer)}</span>
                      {!printer.deleted && !printer.is_active ? (
                        <span
                          className={`mt-0.5 inline-block rounded border border-bambu-dark-tertiary px-1 text-[10px] ${SECONDARY_TEXT_CLASS}`}
                        >
                          {t('fleetMetrics.matrix.deactivated')}
                        </span>
                      ) : null}
                      {rowControl ? (
                        <button
                          type="button"
                          ref={register(row, COL_PRINTER)}
                          {...cellPlumbing(row, COL_PRINTER)}
                          onKeyDown={handleKeyDown}
                          onClick={() =>
                            openDetail(
                              printer.printer_id,
                              activeCol >= FIRST_BUCKET_COL
                                ? activeCol - FIRST_BUCKET_COL
                                : buckets.length - 1,
                            )
                          }
                          aria-label={`${t('fleetMetrics.matrix.details')} ${nameOf(printer)}`}
                          className="mt-0.5 block rounded border border-bambu-dark-tertiary px-1 text-[10px] text-bambu-gray-light hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset"
                        >
                          {t('fleetMetrics.matrix.details')}
                        </button>
                      ) : null}
                    </th>
                    <FrozenFigure
                      column={COL_TOTAL}
                      value={figures.total}
                      lens={activeLens}
                      kind="total"
                      plumbing={cellPlumbing(row, COL_TOTAL)}
                      cellRef={register(row, COL_TOTAL)}
                      onKeyDown={handleKeyDown}
                    />
                    <FrozenFigure
                      column={COL_AVG}
                      value={figures.avg}
                      lens={activeLens}
                      kind="avg"
                      plumbing={cellPlumbing(row, COL_AVG)}
                      cellRef={register(row, COL_AVG)}
                      onKeyDown={handleKeyDown}
                    />
                    <FleetMatrixCell
                      cell={totalsCell}
                      header={windowHeader}
                      lens="time_split"
                      bucketWidth={bucketWidth}
                      mode={resolvedMode}
                      printsMax={printsMax}
                      heat={false}
                      valueHidden={false}
                      width={SPLIT_COLUMN_WIDTH_PX}
                      cellRef={register(row, COL_SPLIT)}
                      {...cellPlumbing(row, COL_SPLIT)}
                      onKeyDown={handleKeyDown}
                      className="@2xl:hidden border-b border-bambu-dark-tertiary"
                    />
                    {buckets.map((bucket, index) => (
                      <FleetMatrixCell
                        key={bucket.start}
                        cell={bucket.values.printers[String(printer.printer_id)]}
                        header={bucket}
                        lens={activeLens}
                        bucketWidth={bucketWidth}
                        mode={resolvedMode}
                        printsMax={printsMax}
                        heat
                        valueHidden={rowControl}
                        width={columnWidth}
                        cellRef={register(row, FIRST_BUCKET_COL + index)}
                        {...cellPlumbing(row, FIRST_BUCKET_COL + index)}
                        onKeyDown={handleKeyDown}
                        onClick={() => openDetail(printer.printer_id, index)}
                        className={bucketClass('border-b border-bambu-dark-tertiary')}
                      />
                    ))}
                  </tr>
                );
              })}
            </tbody>

            <tfoot>
              <tr>
                <th
                  scope="row"
                  ref={register(rowCount - 1, COL_PRINTER)}
                  {...cellPlumbing(rowCount - 1, COL_PRINTER)}
                  onKeyDown={handleKeyDown}
                  className={`${FROZEN_CLASS[COL_PRINTER]} px-1 py-1 font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset`}
                >
                  {t('fleetMetrics.matrix.fleetRow')}
                </th>
                <FrozenFigure
                  column={COL_TOTAL}
                  value={rowFigures(activeLens, totals.fleet, fleetPerDay(activeLens, sorted)).total}
                  lens={activeLens}
                  kind="total"
                  plumbing={cellPlumbing(rowCount - 1, COL_TOTAL)}
                  cellRef={register(rowCount - 1, COL_TOTAL)}
                  onKeyDown={handleKeyDown}
                />
                <FrozenFigure
                  column={COL_AVG}
                  value={fleetPerDay(activeLens, sorted)}
                  lens={activeLens}
                  kind="avg"
                  plumbing={cellPlumbing(rowCount - 1, COL_AVG)}
                  cellRef={register(rowCount - 1, COL_AVG)}
                  onKeyDown={handleKeyDown}
                />
                <FleetMatrixCell
                  cell={totals.fleet}
                  header={windowHeader}
                  lens="time_split"
                  bucketWidth={bucketWidth}
                  mode={resolvedMode}
                  printsMax={printsMax}
                  heat={false}
                  valueHidden={false}
                  width={SPLIT_COLUMN_WIDTH_PX}
                  cellRef={register(rowCount - 1, COL_SPLIT)}
                  {...cellPlumbing(rowCount - 1, COL_SPLIT)}
                  onKeyDown={handleKeyDown}
                  className="@2xl:hidden"
                />
                {buckets.map((bucket, index) => (
                  <FleetMatrixCell
                    key={bucket.start}
                    cell={bucket.values.fleet}
                    header={bucket}
                    lens={activeLens}
                    bucketWidth={bucketWidth}
                    mode={resolvedMode}
                    printsMax={printsMax}
                    heat={false}
                    valueHidden={rowControl}
                    width={columnWidth}
                    cellRef={register(rowCount - 1, FIRST_BUCKET_COL + index)}
                    {...cellPlumbing(rowCount - 1, FIRST_BUCKET_COL + index)}
                    onKeyDown={handleKeyDown}
                    className={bucketClass()}
                  />
                ))}
              </tr>
            </tfoot>
          </table>
        </div>
      </TabPanel>

      {detail && detailPrinter && detailBucket ? (
        <BucketDetail
          printer={detailPrinter}
          cell={detailBucket.values.printers[String(detail.printerId)]}
          seriesBucket={detailBucket}
          bucketWidth={bucketWidth}
          tzName={overview.tz_name}
          onClose={() => setDetail(null)}
        />
      ) : null}
    </div>
  );
}

/** Nulls last in both directions: "no denominator" is not "smallest". */
function compareFigures(a: number | null, b: number | null, direction: SortDirection): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return direction === 'asc' ? a - b : b - a;
}

/**
 * The fleet row's Avg: the fleet-wide per-day figure, which IS the printers'
 * own per-day rates added up. Null only when no printer has a denominator yet.
 */
function fleetPerDay(lens: FleetLens, printers: MatrixPrinter[]): number | null {
  let total: number | null = null;
  for (const printer of printers) {
    const value = lens === 'prints' ? printer.prints_per_day : printer.hours_down_per_day;
    if (value === null) continue;
    total = (total ?? 0) + value;
  }
  return total;
}

interface FrozenFigureProps {
  column: number;
  value: number | null;
  lens: FleetLens;
  kind: 'total' | 'avg';
  plumbing: { tabIndex: number; onFocus: () => void };
  cellRef: (element: HTMLElement | null) => void;
  onKeyDown: (event: KeyboardEvent<HTMLElement>) => void;
}

/** One frozen figure cell — Total or Avg, formatted in the active lens's unit. */
function FrozenFigure({ column, value, lens, kind, plumbing, cellRef, onKeyDown }: FrozenFigureProps) {
  const { i18n } = useTranslation();
  const locale = i18n.language;

  let text = '–';
  if (value !== null) {
    if (lens === 'prints') text = kind === 'total' ? formatCount(value, locale) : formatPrinters(value, locale);
    else if (lens === 'hours_down') text = formatHours(value, locale);
    else text = kind === 'total' ? formatPercent(value, locale) : formatHours(value, locale);
  }

  return (
    <td
      ref={cellRef}
      {...plumbing}
      onKeyDown={onKeyDown}
      className={`${FROZEN_CLASS[column]} ${MATRIX_CELL_BASE} border-b border-bambu-dark-tertiary`}
    >
      {text}
    </td>
  );
}

/** The top header line: weekday initial by day, the hour by hour, blank by week. */
function topLine(bucketWidth: string, label: BucketLabel): string {
  if (bucketWidth === 'hour') return label.hour?.slice(0, 2) ?? '';
  if (bucketWidth === 'week') return ' ';
  return label.weekdayInitial;
}

/** The bottom line: day of month — on the hour lens, only where the day turns over. */
function bottomLine(bucketWidth: string, label: BucketLabel, newDay: boolean): string {
  if (bucketWidth === 'hour') return newDay ? label.dayOfMonth : ' ';
  return label.dayOfMonth;
}

