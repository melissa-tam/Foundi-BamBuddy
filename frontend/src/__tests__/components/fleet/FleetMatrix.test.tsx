/**
 * FleetMatrix — grid semantics, the keyboard model, sorting, the three lenses,
 * the column set per echoed bucket, and the four absence treatments.
 *
 * Everything is queried by role or by the text a user (or a screen reader)
 * actually gets. The one structural helper — `cellAt` — walks the table's own
 * rows and cells, because "arrowing right moves one column" is a claim about
 * the grid's structure and nothing else can state it.
 *
 * Expected copy comes from `i18n.t`, never from a literal, so a copy edit
 * moves the test with it instead of breaking it.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import userEvent from '@testing-library/user-event';
import { render, screen, waitFor, within } from '../../utils';
import { formatCount, formatPrinters } from '../../../utils/fleetMetrics';
import i18n from '../../../i18n';
import { FleetMatrix } from '../../../components/fleet/FleetMatrix';
import {
  DELETED_PRINTER_ID,
  HEALTHY_PRINTER_ID,
  PROBLEM_PRINTER_ID,
  makeFleetOverview,
  makeFleetOverviewDay,
  makeFleetOverviewFirstRun,
  makeFleetOverviewHour,
  makeFleetOverviewWeek,
  makeFleetStatus,
  makeFleetStatusFirstRun,
} from '../../fixtures/fleetMetrics';
import type { FleetOverview } from '../../../types/fleetMetrics';

const t = (key: string, options?: Record<string, unknown>): string =>
  i18n.t(key, options as never) as string;

const LENS_PRINTS = () => t('fleetMetrics.matrix.lens.prints');
const LENS_HOURS = () => t('fleetMetrics.matrix.lens.hoursDown');
const LENS_SPLIT = () => t('fleetMetrics.matrix.lens.timeSplit');

/** Logical grid rows: the header row, then the printers, then the fleet row. */
function gridRows(grid: HTMLElement): HTMLTableRowElement[] {
  return [
    ...grid.querySelectorAll<HTMLTableRowElement>('thead tr'),
    ...grid.querySelectorAll<HTMLTableRowElement>('tbody tr'),
    ...grid.querySelectorAll<HTMLTableRowElement>('tfoot tr'),
  ];
}

/** The `<col>` that sizes one column. Widths are the colgroup's alone. */
function colAt(grid: HTMLElement, col: number): HTMLTableColElement {
  const element = grid.querySelectorAll('col')[col];
  if (!(element instanceof HTMLTableColElement)) throw new Error(`no col at ${col}`);
  return element;
}

function cellAt(grid: HTMLElement, row: number, col: number): HTMLElement {
  const cell = gridRows(grid)[row]?.children[col];
  if (!(cell instanceof HTMLElement)) throw new Error(`no cell at ${row},${col}`);
  return cell;
}

/** The element that actually takes focus — the cell, or the widget inside it. */
function focusTarget(cell: HTMLElement): HTMLElement {
  return cell.matches('[tabindex]') ? cell : (cell.querySelector<HTMLElement>('[tabindex]') ?? cell);
}

function theGrid(): HTMLElement {
  return screen.getByRole('grid');
}

/** Index of the printer row whose header names `label`. */
function rowOf(grid: HTMLElement, label: string | RegExp): number {
  const rows = gridRows(grid);
  const index = rows.findIndex((row) => {
    const header = row.querySelector('th[scope="row"]');
    if (!header?.textContent) return false;
    return typeof label === 'string' ? header.textContent.includes(label) : label.test(header.textContent);
  });
  if (index < 0) throw new Error(`no row for ${String(label)}`);
  return index;
}

/**
 * The frozen block is three columns wide — Printer · Total · Avg — and the
 * bucket columns begin immediately after it. There is no fourth "Time split"
 * column: a column between the frozen block and the buckets cannot be hidden
 * at the wide breakpoint without shifting every bucket cell off its `<col>`,
 * so the phone's split bar lives inside the printer cell instead.
 */
const FIRST_BUCKET_COL = 3;
const COL_TOTAL = 1;
const COL_AVG = 2;

async function switchLens(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole('tab', { name }));
}

describe('FleetMatrix', () => {
  let user: ReturnType<typeof userEvent.setup>;

  beforeEach(() => {
    user = userEvent.setup();
  });

  describe('grid semantics', () => {
    it('is a real grid with a caption, column headers and row headers', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();

      expect(grid).toHaveAccessibleName(
        t('fleetMetrics.matrix.caption', {
          lens: LENS_PRINTS(),
          from: 'Sep 15, 2026',
          to: 'Sep 21, 2026',
        }),
      );
      // "by printer", never "per printer": the tab has a metric called "Prints
      // per printer", and the Prints-lens caption used to name that rate.
      expect(grid.querySelector('caption')?.textContent).not.toContain('per printer');
      // The three frozen columns plus seven day buckets.
      expect(within(grid).getAllByRole('columnheader')).toHaveLength(FIRST_BUCKET_COL + 7);
      // Three printers (the deleted one is hidden) plus the fleet row.
      expect(within(grid).getAllByRole('rowheader')).toHaveLength(4);
    });

    it('gives no cell an aria-label, so the header association survives', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      expect(theGrid().querySelectorAll('td[aria-label], th[aria-label]')).toHaveLength(0);
    });

    it('keeps the fleet row last under every sort', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      const fleetLabel = t('fleetMetrics.matrix.fleetRow');
      const lastRow = () => gridRows(grid).at(-1)?.querySelector('th[scope="row"]')?.textContent;

      expect(lastRow()).toContain(fleetLabel);
      await user.click(
        screen.getByRole('button', {
          name: t('fleetMetrics.matrix.sortBy', { column: t('fleetMetrics.matrix.columns.printer') }),
        }),
      );
      expect(lastRow()).toContain(fleetLabel);
    });
  });

  describe('keyboard', () => {
    it('is exactly one tab stop', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      expect(grid.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
      // Every widget inside is taken out of the tab order by the roving index.
      for (const button of grid.querySelectorAll('button')) {
        expect(button).toHaveAttribute('tabindex');
      }
    });

    it('moves one cell per arrow key', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL)).focus();

      await user.keyboard('{ArrowRight}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL + 1)));
      await user.keyboard('{ArrowDown}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 2, FIRST_BUCKET_COL + 1)));
      await user.keyboard('{ArrowLeft}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 2, FIRST_BUCKET_COL)));
      await user.keyboard('{ArrowUp}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL)));
    });

    it('reaches the row ends with Home and End and the grid corners with Ctrl', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      const lastCol = FIRST_BUCKET_COL + 7 - 1;
      const lastRow = gridRows(grid).length - 1;
      focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL + 2)).focus();

      await user.keyboard('{End}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, lastCol)));
      await user.keyboard('{Home}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, 0)));
      await user.keyboard('{Control>}{End}{/Control}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, lastRow, lastCol)));
      await user.keyboard('{Control>}{Home}{/Control}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 0, 0)));
    });

    it('moves ten columns per page key, clamped at the ends', async () => {
      // A 30-day window has more than ten bucket columns to move across.
      render(<FleetMatrix overview={makeFleetOverview({ bucket: 'day', count: 30 })} />);
      const grid = theGrid();
      const lastCol = FIRST_BUCKET_COL + 30 - 1;
      focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL)).focus();

      await user.keyboard('{PageDown}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL + 10)));
      await user.keyboard('{PageUp}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL)));
      await user.keyboard('{PageUp}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, 0)));
      await user.keyboard('{End}');
      expect(document.activeElement).toBe(focusTarget(cellAt(grid, 1, lastCol)));
    });

    it.each([['{Enter}'], ['[Space]']])(
      'opens the bucket detail with %s and returns focus on Escape',
      async (key) => {
        render(<FleetMatrix overview={makeFleetOverviewDay()} />);
        const grid = theGrid();
        const cell = focusTarget(cellAt(grid, 1, FIRST_BUCKET_COL + 4));
        cell.focus();

        await user.keyboard(key);
        expect(await screen.findByRole('dialog')).toBeInTheDocument();

        await user.keyboard('{Escape}');
        await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
        expect(document.activeElement).toBe(cell);
      },
    );
  });

  describe('sorting', () => {
    const sortName = (column: string) =>
      t('fleetMetrics.matrix.sortBy', { column: t(`fleetMetrics.matrix.columns.${column}`) });

    it('defaults to worst-first on the active lens', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const totalHeader = () => screen.getByRole('button', { name: sortName('total') }).closest('th');

      // Prints: worst is the FEWEST, so the total column sorts ascending.
      expect(totalHeader()).toHaveAttribute('aria-sort', 'ascending');

      await switchLens(user, LENS_HOURS());
      // Hours down: worst is the MOST.
      expect(totalHeader()).toHaveAttribute('aria-sort', 'descending');

      await switchLens(user, LENS_SPLIT());
      // Time split: worst is the LOWEST printing share.
      expect(totalHeader()).toHaveAttribute('aria-sort', 'ascending');
    });

    it('puts the worst printer first and flips on a second press', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      const firstRowName = () => gridRows(grid)[1]?.querySelector('th[scope="row"]')?.textContent ?? '';

      await switchLens(user, LENS_HOURS());
      // 009-H2C is the fixture's problem printer: the most hours down.
      expect(firstRowName()).toContain('009-H2C');

      await user.click(screen.getByRole('button', { name: sortName('total') }));
      expect(screen.getByRole('button', { name: sortName('total') }).closest('th')).toHaveAttribute(
        'aria-sort',
        'ascending',
      );
      expect(firstRowName()).not.toContain('009-H2C');
    });

    it('sorts by printer name and marks only that column', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      await user.click(screen.getByRole('button', { name: sortName('printer') }));

      expect(screen.getByRole('button', { name: sortName('printer') }).closest('th')).toHaveAttribute(
        'aria-sort',
        'ascending',
      );
      expect(screen.getByRole('button', { name: sortName('total') }).closest('th')).toHaveAttribute(
        'aria-sort',
        'none',
      );
      expect(gridRows(theGrid())[1]?.querySelector('th[scope="row"]')?.textContent).toContain('001-H2S');
    });
  });

  describe('lenses', () => {
    it('switches the legend and the cell encoding', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      const problemRow = rowOf(grid, '009-H2C');

      expect(screen.getByText(/^0 to /)).toBeInTheDocument();
      const printsCell = cellAt(grid, problemRow, FIRST_BUCKET_COL + 4).textContent;

      await switchLens(user, LENS_HOURS());
      expect(screen.getByText(t('fleetMetrics.matrix.legend.over', { hours: '12' }))).toBeInTheDocument();
      expect(cellAt(theGrid(), rowOf(theGrid(), '009-H2C'), FIRST_BUCKET_COL + 4).textContent).not.toBe(
        printsCell,
      );

      await switchLens(user, LENS_SPLIT());
      expect(screen.getByText(t('fleetMetrics.matrix.legend.printingBand'))).toBeInTheDocument();
      expect(screen.getByText(t('fleetMetrics.matrix.legend.downBand'))).toBeInTheDocument();
    });
  });

  describe('the column set follows the echoed bucket', () => {
    it('renders 28 px day columns with a weekday initial, the month at each change and Today', () => {
      // 30 days ending 21 Sep 2026 crosses back into August.
      render(<FleetMatrix overview={makeFleetOverview({ bucket: 'day', count: 30 })} />);
      const grid = theGrid();

      expect(cellAt(grid, 0, FIRST_BUCKET_COL).style.width).toBe('28px');
      expect(within(grid).getAllByText('Aug')).toHaveLength(1);
      expect(within(grid).getAllByText('Sep')).toHaveLength(1);
      expect(within(grid).getAllByText(t('fleetMetrics.units.today')).length).toBeGreaterThan(0);
      // Each bucket header still spells its full site date for a screen reader.
      expect(cellAt(grid, 0, FIRST_BUCKET_COL + 29).textContent).toContain('Sep 21, 2026');
    });

    it('renders 40 px week columns labelled by their start date', () => {
      render(<FleetMatrix overview={makeFleetOverviewWeek()} />);
      const grid = theGrid();

      expect(within(grid).getAllByRole('columnheader')).toHaveLength(FIRST_BUCKET_COL + 6);
      // Widths live on the `<colgroup>` — one owner, and the sticky offsets are
      // summed from the same constants.
      expect(colAt(grid, FIRST_BUCKET_COL).style.width).toBe('40px');
      expect(
        within(grid).getAllByText(t('fleetMetrics.units.weekOf', { date: 'Sep 21, 2026' }), {
          exact: false,
        }).length,
      ).toBeGreaterThan(0);
    });

    it('captions the grid for whichever lens is up', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const caption = (): string => theGrid().querySelector('caption')?.textContent ?? '';

      expect(caption()).toContain(LENS_PRINTS());
      await switchLens(user, LENS_HOURS());
      expect(caption()).toContain(LENS_HOURS());
      expect(caption()).not.toContain(LENS_PRINTS());
      await switchLens(user, LENS_SPLIT());
      expect(caption()).toContain(LENS_SPLIT());
      // Whichever lens it names, it names the GRID, never the rate metric.
      expect(caption()).not.toContain('per printer');
    });

    it('captions a ONE-DAY window with its single date', () => {
      const overview = makeFleetOverview({ bucket: 'hour', count: 12 });
      overview.date_from = overview.date_to;
      render(<FleetMatrix overview={overview} />);

      const caption = theGrid().querySelector('caption')?.textContent ?? '';
      expect(caption).toContain('Sep 21, 2026');
      // Not "Sep 21, 2026 to Sep 21, 2026" — one date, said once.
      expect(caption.match(/Sep 21, 2026/g)).toHaveLength(1);
    });

    it('spells only every third hour header, and all of them for a screen reader', () => {
      // 14 px columns: labelling all twenty-four ran them into `000102030405…`.
      render(<FleetMatrix overview={makeFleetOverview({ bucket: 'hour', count: 24 })} />);
      const grid = theGrid();
      const headers = [...grid.querySelectorAll('thead th')].slice(FIRST_BUCKET_COL);
      expect(headers).toHaveLength(24);

      // A header's aria-hidden spans are [optional month-or-Today overlay,
      // top line, bottom line] — the hour is the top line, second from last.
      const visible = headers.map((header) => {
        const spans = header.querySelectorAll('[aria-hidden="true"]');
        return spans[spans.length - 2]?.textContent?.trim() ?? '';
      });
      const labelled = visible.filter((text) => text !== '');
      // The window ends at 09:00, so it opens at 10:00 the day before.
      expect(labelled).toEqual(['12', '15', '18', '21', '00', '03', '06', '09']);

      // Every one of the twenty-four still carries its full site stamp.
      for (const header of headers) {
        expect(header.querySelector('.sr-only')?.textContent ?? '').toMatch(/\d{2}:\d{2}/);
      }
    });

    it('renders 14 px hour columns and opens the detail from the row control', async () => {
      render(<FleetMatrix overview={makeFleetOverviewHour()} />);
      const grid = theGrid();

      expect(cellAt(grid, 0, FIRST_BUCKET_COL).style.width).toBe('14px');
      // The 14 px cell shows no figure; the value rides sr-only text instead.
      const controls = within(grid).getAllByRole('button', {
        name: new RegExp(t('fleetMetrics.matrix.details')),
      });
      expect(controls.length).toBe(3);

      await user.click(controls[0]!);
      expect(await screen.findByRole('dialog')).toBeInTheDocument();
    });
  });

  describe('the four absences', () => {
    it('renders a dim zero for a bucket the farm genuinely had nothing in', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      // Bucket 0 predates the recorder: no prints happened, and the Prints
      // lens is fed by the complete print log, so the honest answer is zero.
      const cell = cellAt(grid, rowOf(grid, '001-H2S'), FIRST_BUCKET_COL);
      expect(cell.textContent).toBe('0');
    });

    it('renders a dash and names the reason before recording started', async () => {
      render(
        <FleetMatrix overview={makeFleetOverviewFirstRun()} status={makeFleetStatusFirstRun()} />,
      );
      await switchLens(user, LENS_HOURS());
      const grid = theGrid();
      const cell = cellAt(grid, rowOf(grid, '001-H2S'), FIRST_BUCKET_COL);

      expect(within(cell).getByText(t('fleetMetrics.class.not_recorded'))).toBeInTheDocument();
      expect(cell.textContent).toContain('–');
    });

    it('names each partly-observed bucket for what it actually is', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      await switchLens(user, LENS_HOURS());
      const grid = theGrid();
      const row = rowOf(grid, '001-H2S');

      // Buckets 0 and 1 have no observation at all: the ledger is the evidence.
      expect(
        within(cellAt(grid, row, FIRST_BUCKET_COL)).getByText(
          t('fleetMetrics.class.incidents_only'),
        ),
      ).toBeInTheDocument();
      // Bucket 3 is half observed — a DIFFERENT claim, and it used to borrow
      // the "No data" leaf from the bucket's `basis` instead of naming itself.
      expect(
        within(cellAt(grid, row, FIRST_BUCKET_COL + 3)).getByText(
          t('fleetMetrics.widgets.partlyObserved'),
        ),
      ).toBeInTheDocument();
    });

    it('never says "No data" beside a figure', async () => {
      // Production: a hatched Hours-down cell showing 12 announced "No data".
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      await switchLens(user, LENS_HOURS());
      const grid = theGrid();

      const offenders = gridRows(grid)
        .flatMap((gridRow) => [...gridRow.children].slice(FIRST_BUCKET_COL))
        .filter((cell) => {
          const text = cell.textContent ?? '';
          return text.includes(t('fleetMetrics.class.unobserved')) && /\d/.test(text);
        });
      expect(offenders).toHaveLength(0);
    });

    it('keeps the texture off the digits of a cell that has some', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      await switchLens(user, LENS_HOURS());
      const grid = theGrid();
      const row = rowOf(grid, '001-H2S');

      // Bucket 3 is half observed and still shows its hours: the marker is a
      // band along the bottom edge, not a hatch across the number.
      const withFigure = cellAt(grid, row, FIRST_BUCKET_COL + 3) as HTMLTableCellElement;
      expect(withFigure.textContent).toMatch(/\d/);
      expect(withFigure.style.backgroundPosition).toBe('left bottom');
      expect(withFigure.style.backgroundRepeat).toBe('no-repeat');
      expect(withFigure.style.backgroundSize).not.toBe('auto');
    });

    it('renders flat ground and names a printer that was out of the fleet', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      await switchLens(user, LENS_HOURS());
      const grid = theGrid();
      const cell = cellAt(grid, rowOf(grid, '011-H2S'), FIRST_BUCKET_COL + 4);

      expect(within(cell).getByText(t('fleetMetrics.class.out_of_fleet'))).toBeInTheDocument();
      expect(cell.textContent).toBe(t('fleetMetrics.class.out_of_fleet'));
    });

    it('never hatches or dashes the Prints lens', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      // Only the BUCKET cells: the phone Time-split column is always the
      // Time-split lens, and hatching it when the recorder fell short is right.
      const reasons = [
        t('fleetMetrics.class.incidents_only'),
        t('fleetMetrics.class.unobserved'),
        '–',
      ];
      const bucketText = (): string =>
        gridRows(theGrid())
          .flatMap((row) => [...row.children].slice(FIRST_BUCKET_COL))
          .map((cell) => cell.textContent ?? '')
          .join('|');

      for (const reason of reasons) expect(bucketText()).not.toContain(reason);

      await switchLens(user, LENS_HOURS());
      expect(bucketText()).toContain(t('fleetMetrics.class.incidents_only'));
    });
  });

  describe('rows', () => {
    it('chips a deactivated printer and keeps its row', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const grid = theGrid();
      const header = gridRows(grid)[rowOf(grid, '011-H2S')]?.querySelector('th[scope="row"]');

      expect(header).not.toBeNull();
      expect(within(header as HTMLElement).getByText(t('fleetMetrics.matrix.deactivated'))).toBeInTheDocument();
    });

    it('omits a deleted printer that was out of the fleet all window', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      expect(within(theGrid()).queryByRole('rowheader', { name: /Printer 9/ })).not.toBeInTheDocument();
    });

    it('names a deleted printer by its id when it still has history', () => {
      render(<FleetMatrix overview={withVisibleDeletedPrinter()} />);
      const grid = theGrid();

      expect(
        within(grid).getByRole('rowheader', {
          name: new RegExp(t('fleetMetrics.matrix.deletedPrinter', { id: DELETED_PRINTER_ID })),
        }),
      ).toBeInTheDocument();
      expect(within(grid).queryByText('gone-from-roster')).not.toBeInTheDocument();
    });
  });

  describe('the Prints lens, on one numerator', () => {
    it('agrees on COMPLETED prints across the cell, the Total and the Avg', () => {
      // `009-H2C` is the fixture's problem printer and really does fail prints,
      // so summing every outcome gives a different answer from counting the
      // completed ones — which is exactly the mismatch this pins.
      const overview = makeFleetOverviewDay();
      const key = String(PROBLEM_PRINTER_ID);
      const cells = overview.matrix.series.buckets.map((bucket) => bucket.values.printers[key]);
      const completed = cells.reduce((sum, cell) => sum + (cell?.prints.completed ?? 0), 0);
      const everyOutcome = cells.reduce(
        (sum, cell) => sum + Object.values(cell?.prints ?? {}).reduce((a, b) => a + (b ?? 0), 0),
        0,
      );
      expect(everyOutcome).toBeGreaterThan(completed);

      render(<FleetMatrix overview={overview} />);
      const grid = theGrid();
      const row = rowOf(grid, '009-H2C');
      const printer = overview.matrix.printers.find((p) => p.printer_id === PROBLEM_PRINTER_ID);

      // The row Total counts completed only…
      expect(cellAt(grid, row, COL_TOTAL).textContent).toBe(
        formatCount(completed, i18n.language),
      );
      // …each bucket cell shows that bucket's completed count…
      const firstCompleted = cells[0]?.prints.completed ?? 0;
      expect(cellAt(grid, row, FIRST_BUCKET_COL).textContent).toContain(
        formatCount(firstCompleted, i18n.language),
      );
      // …and Avg is the payload's own per-day rate, which the backend computes
      // from completed prints. Three surfaces, one numerator.
      expect(cellAt(grid, row, COL_AVG).textContent).toBe(
        formatPrinters(printer?.prints_per_day ?? 0, i18n.language),
      );
    });

    it('states on the control that it means completed prints', () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      expect(
        screen.getByRole('button', { name: t('fleetMetrics.matrix.lensHint.prints') }),
      ).toBeInTheDocument();
    });
  });

  describe('first run', () => {
    it('opens on Prints and disables Time split with its reason reachable', () => {
      render(
        <FleetMatrix overview={makeFleetOverviewFirstRun()} status={makeFleetStatusFirstRun()} />,
      );

      expect(screen.getByRole('tab', { selected: true })).toHaveAccessibleName(LENS_PRINTS());
      expect(screen.getByRole('tab', { name: LENS_SPLIT() })).toHaveAttribute('aria-disabled', 'true');
      expect(screen.getByRole('button', { name: /Needs recorded printer state/i })).toBeInTheDocument();
    });

    it('says nothing has EVER been recorded rather than naming a date', () => {
      // `recording_since` is null on a first run, and the reason used to fall
      // back to the window's own end date — which is not when recording
      // started, and on a fresh instance is a date nothing was recorded on.
      render(
        <FleetMatrix overview={makeFleetOverviewFirstRun()} status={makeFleetStatusFirstRun()} />,
      );

      expect(
        screen.getByRole('button', { name: t('fleetMetrics.matrix.timeSplitDisabledNoData') }),
      ).toBeInTheDocument();
    });

    it('names the recorder start date, in the SITE zone, once there is one', () => {
      // A window that predates the recorder: nothing observed in it, but
      // recording HAS started, so the reason can say when.
      const overview = makeFleetOverviewFirstRun();
      const status = makeFleetStatus();

      render(<FleetMatrix overview={overview} status={status} />);

      // `recording_since` is 2026-09-01T03:00:00 naive UTC; the fixture site is
      // Pacific/Auckland, where that instant is already 2026-09-01 15:00.
      expect(
        screen.getByRole('button', {
          name: t('fleetMetrics.matrix.timeSplitDisabled', { date: 'Sep 1, 2026' }),
        }),
      ).toBeInTheDocument();
    });
  });

  describe('phone disclosure', () => {
    it('toggles the bucket columns behind an aria-expanded control', async () => {
      render(<FleetMatrix overview={makeFleetOverviewDay()} />);
      const show = screen.getByRole('button', { name: t('fleetMetrics.matrix.showDays') });

      expect(show).toHaveAttribute('aria-expanded', 'false');
      await user.click(show);

      const hide = screen.getByRole('button', { name: t('fleetMetrics.matrix.hideDays') });
      expect(hide).toHaveAttribute('aria-expanded', 'true');
      await user.click(hide);
      expect(screen.getByRole('button', { name: t('fleetMetrics.matrix.showDays') })).toHaveAttribute(
        'aria-expanded',
        'false',
      );
    });
  });
});

/**
 * The deleted printer, given the healthy printer's history so it is NOT hidden,
 * and a roster name that would betray a component reading `name` instead of the
 * "Printer {{id}}" fallback.
 */
function withVisibleDeletedPrinter(): FleetOverview {
  const overview = makeFleetOverviewDay();
  const deleted = String(DELETED_PRINTER_ID);
  const healthy = String(HEALTHY_PRINTER_ID);
  for (const bucket of overview.matrix.series.buckets) {
    const donor = bucket.values.printers[healthy];
    if (donor) bucket.values.printers[deleted] = donor;
  }
  const donorTotals = overview.matrix.series.totals.printers[healthy];
  if (donorTotals) overview.matrix.series.totals.printers[deleted] = donorTotals;
  overview.matrix.printers = overview.matrix.printers.map((printer) =>
    printer.deleted ? { ...printer, name: 'gone-from-roster' } : printer,
  );
  return overview;
}
