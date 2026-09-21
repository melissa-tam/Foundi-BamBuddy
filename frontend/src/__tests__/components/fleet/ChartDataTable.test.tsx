/**
 * The shared chart data table — the accessible alternative behind every
 * "Show data" button on the Fleet tab.
 *
 * What matters here is that it is a REAL table: headers that associate, a
 * caption that says what it is, a footer that is marked as totals rather than
 * as one more bucket, and a partly observed row that says so in words and not
 * only in a texture. Everything is queried by role and by accessible name.
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
  { key: 'b', header: 'Tue 15 Sep', partial: true, values: { hours: 0, prints: 9 } },
];

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

    const partial = screen
      .getByRole('rowheader', { name: new RegExp('Tue 15 Sep') })
      .closest('tr') as HTMLElement;
    expect(
      within(partial).getByRole('img', { name: i18n.t('fleetMetrics.class.unobserved') }),
    ).toBeInTheDocument();
  });

  it('leaves a fully observed row unmarked', () => {
    renderTable();

    const full = screen
      .getByRole('rowheader', { name: new RegExp('Mon 14 Sep') })
      .closest('tr') as HTMLElement;
    expect(within(full).queryByRole('img')).toBeNull();
  });

  it('explains the marker through a hint rather than inline copy', () => {
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
