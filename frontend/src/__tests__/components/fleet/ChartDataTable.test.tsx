/**
 * The shared chart data table — the accessible alternative behind every
 * "Show data" button on the Fleet tab.
 *
 * What matters here is that it is a REAL table: headers that associate, a
 * caption that says what it is, a footer that is marked as totals rather than
 * as one more bucket, and — the point of the table existing at all — a row that
 * understates saying WHICH WAY it understates, in words and not only in a
 * texture. A bucket still running and a bucket the recorder only partly watched
 * are different claims, and this is the surface that can afford to tell them
 * apart. Everything is queried by role and by accessible name.
 */
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import i18n from '../../../i18n';
import { ChartDataTable, type ChartDataColumn } from '../../../components/fleet/ChartDataTable';

interface Row {
  hours: number;
  prints: number;
}

const columns: ChartDataColumn<Row>[] = [
  { key: 'hours', header: 'Hours down', unit: 'h', format: (row) => row.hours.toFixed(1) },
  { key: 'prints', header: 'Prints', format: (row) => String(row.prints) },
];

const rows = [
  { key: 'a', header: 'Mon 14 Sep', values: { hours: 1.5, prints: 12 } },
  { key: 'b', header: 'Tue 15 Sep', partlyObserved: true, values: { hours: 0, prints: 9 } },
  { key: 'c', header: 'Wed 16 Sep', inProgress: true, values: { hours: 0.2, prints: 4 } },
];

const inProgressName = (): string => i18n.t('fleetMetrics.widgets.inProgress');
const partlyObservedName = (): string => i18n.t('fleetMetrics.widgets.partlyObserved');

const rowNamed = (name: string): HTMLElement =>
  screen.getByRole('rowheader', { name: new RegExp(name) }).closest('tr') as HTMLElement;

function renderTable(overrides: Partial<Parameters<typeof ChartDataTable<Row>>[0]> = {}) {
  return render(
    <ChartDataTable<Row>
      caption="Downtime by cause"
      rowHeader="Date"
      columns={columns}
      rows={rows}
      {...overrides}
    />,
  );
}

describe('ChartDataTable', () => {
  it('describes itself with a caption', () => {
    renderTable();
    expect(screen.getByRole('table')).toHaveAccessibleName('Downtime by cause');
  });

  it('heads every column, carrying the unit with the heading', () => {
    renderTable();

    expect(screen.getByRole('columnheader', { name: 'Date' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Hours down (h)' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Prints' })).toBeInTheDocument();
  });

  it('heads every row and prints the values its columns formatted', () => {
    renderTable();

    const row = screen.getByRole('rowheader', { name: /Mon 14 Sep/ }).closest('tr');
    expect(row).not.toBeNull();
    const cells = within(row as HTMLElement).getAllByRole('cell');
    expect(cells.map((cell) => cell.textContent)).toEqual(['1.5', '12']);
  });

  it('marks a partly observed row with a NAMED marker, not texture alone', () => {
    renderTable();

    expect(
      within(rowNamed('Tue 15 Sep')).getByRole('img', { name: partlyObservedName() }),
    ).toBeInTheDocument();
  });

  it('marks a row whose bucket is still running as in progress, not as unobserved', () => {
    renderTable();

    const running = rowNamed('Wed 16 Sep');
    expect(within(running).getByRole('img', { name: inProgressName() })).toBeInTheDocument();
    // The two are different claims: nobody failed to watch this bucket, it
    // simply has not finished. Marking it "partly observed" would report a
    // working recorder as a broken one.
    expect(within(running).queryByRole('img', { name: partlyObservedName() })).toBeNull();
  });

  it('keeps the partly observed marker off a row that is only unfinished, and vice versa', () => {
    renderTable();

    expect(
      within(rowNamed('Tue 15 Sep')).queryByRole('img', { name: inProgressName() }),
    ).toBeNull();
  });

  it('marks a row that is BOTH unfinished and partly observed with both', () => {
    renderTable({
      rows: [{ key: 'z', header: 'Today', inProgress: true, partlyObserved: true, values: { hours: 0.1, prints: 1 } }],
    });

    const today = rowNamed('Today');
    expect(within(today).getByRole('img', { name: inProgressName() })).toBeInTheDocument();
    expect(within(today).getByRole('img', { name: partlyObservedName() })).toBeInTheDocument();
  });

  it('leaves a fully observed, finished row unmarked', () => {
    renderTable();

    expect(within(rowNamed('Mon 14 Sep')).queryByRole('img')).toBeNull();
  });

  it('explains the recorder marker through a hint rather than inline copy', () => {
    renderTable();
    expect(
      screen.getByRole('button', { name: i18n.t('fleetMetrics.hints.unobserved') }),
    ).toBeInTheDocument();
  });

  it('offers no hint when nothing in the window was partly observed', () => {
    renderTable({ rows: [rows[0]] });
    expect(
      screen.queryByRole('button', { name: i18n.t('fleetMetrics.hints.unobserved') }),
    ).toBeNull();
  });

  it('does not raise the recorder hint for a merely unfinished bucket', () => {
    // The hint explains a BLIND SPOT. A running bucket has none, and offering
    // the explanation anyway would teach the reader to distrust a good number.
    renderTable({ rows: [rows[2]] });
    expect(
      screen.queryByRole('button', { name: i18n.t('fleetMetrics.hints.unobserved') }),
    ).toBeNull();
  });

  it('renders the window figures as a totals row through the SAME formatters', () => {
    renderTable({
      totals: { key: 'window', header: 'Total', values: { hours: 1.5, prints: 21 } },
    });

    const totals = screen.getByRole('rowheader', { name: 'Total' }).closest('tr') as HTMLElement;
    expect(within(totals).getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      '1.5',
      '21',
    ]);
  });

  it('omits the totals row when the widget has no window figure to state', () => {
    renderTable();
    expect(screen.queryByRole('rowheader', { name: 'Total' })).toBeNull();
  });

  it('puts the scrolling region in the tab order, named, so it is reachable without a pointer', () => {
    renderTable();

    const region = screen.getByRole('group', { name: 'Downtime by cause' });
    expect(region).toHaveAttribute('tabindex', '0');
  });
});
