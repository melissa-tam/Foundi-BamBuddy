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
import i18n from '../../../i18n';
import { FleetMatrix } from '../../../components/fleet/FleetMatrix';
import {
  DELETED_PRINTER_ID,
  HEALTHY_PRINTER_ID,
  makeFleetOverview,
  makeFleetOverviewDay,
  makeFleetOverviewFirstRun,
  makeFleetOverviewHour,
  makeFleetOverviewWeek,
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

const FIRST_BUCKET_COL = 4;

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
      // Three frozen columns + the phone Time-split column + seven day buckets.
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
      expect(cellAt(grid, 0, FIRST_BUCKET_COL).style.width).toBe('40px');
      expect(
        within(grid).getAllByText(t('fleetMetrics.units.weekOf', { date: 'Sep 21, 2026' }), {
          exact: false,
        }).length,
      ).toBeGreaterThan(0);
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

    it('hatches and names a bucket the recorder only partly covered', async () => {
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
      // Bucket 3 is half observed.
      expect(
        within(cellAt(grid, row, FIRST_BUCKET_COL + 3)).getByText(t('fleetMetrics.class.unobserved')),
      ).toBeInTheDocument();
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

  describe('first run', () => {
    it('opens on Prints and disables Time split with its reason reachable', () => {
      render(
        <FleetMatrix overview={makeFleetOverviewFirstRun()} status={makeFleetStatusFirstRun()} />,
      );

      expect(screen.getByRole('tab', { selected: true })).toHaveAccessibleName(LENS_PRINTS());
      expect(screen.getByRole('tab', { name: LENS_SPLIT() })).toHaveAttribute('aria-disabled', 'true');
      expect(screen.getByRole('button', { name: /Needs recorded printer state/i })).toBeInTheDocument();
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
