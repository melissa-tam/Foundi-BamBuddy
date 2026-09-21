/**
 * The Fleet tab's widget grid.
 *
 * recharts renders nothing measurable in jsdom, so this file tests what is both
 * testable and load-bearing: that the six widgets are mounted in the grid under
 * the Fleet layout key, that the shared paint servers are defined exactly once
 * for the whole page, that the "Show data" switch actually swaps the plot for
 * the SAME numbers the builders produced, and that a widget with nothing to
 * draw says so instead of drawing an empty axis.
 *
 * Everything is queried by role and accessible name; expected copy is resolved
 * through i18n rather than typed here, so a copy edit does not break a test.
 */
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import i18n from '../../../i18n';
import {
  DEACTIVATED_PRINTER_ID,
  HEALTHY_PRINTER_ID,
  makeFleetOverview,
  makeFleetOverviewFirstRun,
} from '../../fixtures/fleetMetrics';
import { FleetWidgets } from '../../../components/fleet/FleetWidgets';
import { CoolingAndEjectWidget } from '../../../components/fleet/widgets/CoolingAndEjectWidget';
import { DowntimeByCauseWidget } from '../../../components/fleet/widgets/DowntimeByCauseWidget';
import { StateOverTimeWidget } from '../../../components/fleet/widgets/StateOverTimeWidget';
import {
  downtimeCauses,
  downtimeRows,
  type RowOptions,
} from '../../../components/fleet/widgets/rows';
import {
  FLEET_DASHBOARD_STORAGE_KEY,
  FLEET_PATTERN_IDS,
  causeLabelKey,
  formatHours,
} from '../../../utils/fleetMetrics';

const overview = makeFleetOverview({ bucket: 'day' });

const rowOptions = (): RowOptions => ({
  bucket: overview.bucket,
  locale: i18n.language,
  todayLabel: i18n.t('fleetMetrics.units.today'),
});

/** The six sections, in the order the wireframe lays them out. */
const SECTION_KEYS = [
  'fleetMetrics.sections.stateOverTime',
  'fleetMetrics.sections.printsPerDay',
  'fleetMetrics.sections.downtimeByCause',
  'fleetMetrics.sections.coolingAndEject',
  'fleetMetrics.sections.recovery',
  'fleetMetrics.sections.partsBySku',
];

const showData = () => i18n.t('fleetMetrics.widgets.showData');
const showChart = () => i18n.t('fleetMetrics.widgets.showChart');

describe('FleetWidgets — the grid', () => {
  it('mounts all six widgets, in the laid-out order', () => {
    render(<FleetWidgets overview={overview} />);

    const headings = screen.getAllByRole('heading', { level: 3 });
    expect(headings.map((heading) => heading.textContent)).toEqual(
      SECTION_KEYS.map((key) => i18n.t(key)),
    );
  });

  it('persists its layout under the FLEET key, not the Prints tab one', () => {
    render(<FleetWidgets overview={overview} />);

    expect(localStorage.setItem).toHaveBeenCalledWith(
      FLEET_DASHBOARD_STORAGE_KEY,
      expect.any(String),
    );
  });

  it('defines each shared pattern exactly once for the whole grid', () => {
    render(<FleetWidgets overview={overview} />);

    for (const id of Object.values(FLEET_PATTERN_IDS)) {
      expect(document.querySelectorAll(`#${id}`)).toHaveLength(1);
    }
  });

  it('gives every widget a Show data switch, all starting on the chart', () => {
    render(<FleetWidgets overview={overview} />);

    const switches = screen.getAllByRole('button', { name: showData() });
    expect(switches).toHaveLength(SECTION_KEYS.length);
    for (const control of switches) {
      expect(control).toHaveAttribute('aria-pressed', 'false');
    }
  });

  it('names each chart, so the plot is not an unlabelled graphic', () => {
    render(<FleetWidgets overview={overview} />);

    // Five SVG plots are named images; Cooling and eject is drawn in HTML and
    // is a named figure instead, so its own text stays readable.
    expect(
      screen.getByRole('img', { name: i18n.t('fleetMetrics.sections.stateOverTime') }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('figure', { name: i18n.t('fleetMetrics.sections.coolingAndEject') }),
    ).toBeInTheDocument();
  });
});

describe('the Show data switch', () => {
  it('swaps the plot for a table and renames itself', async () => {
    const user = userEvent.setup();
    render(<DowntimeByCauseWidget overview={overview} size={2} />);

    const title = i18n.t('fleetMetrics.sections.downtimeByCause');
    expect(screen.getByRole('img', { name: title })).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();

    await user.click(screen.getByRole('button', { name: showData() }));

    const control = screen.getByRole('button', { name: showChart() });
    expect(control).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('table')).toHaveAccessibleName(title);
    expect(screen.queryByRole('img', { name: title })).toBeNull();
  });

  it('goes back to the plot', async () => {
    const user = userEvent.setup();
    render(<DowntimeByCauseWidget overview={overview} size={2} />);

    await user.click(screen.getByRole('button', { name: showData() }));
    await user.click(screen.getByRole('button', { name: showChart() }));

    expect(
      screen.getByRole('img', { name: i18n.t('fleetMetrics.sections.downtimeByCause') }),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: showData() })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('shows the SAME numbers the chart was drawn from', async () => {
    const user = userEvent.setup();
    const causes = downtimeCauses(overview.fleet_series);
    const rows = downtimeRows(overview.fleet_series, causes, rowOptions());

    render(<DowntimeByCauseWidget overview={overview} size={2} />);
    await user.click(screen.getByRole('button', { name: showData() }));

    // Every cause has a column, headed by the label the legend uses.
    for (const cause of causes) {
      expect(
        screen.getByRole('columnheader', {
          name: new RegExp(i18n.t(causeLabelKey(cause)), 'i'),
        }),
      ).toBeInTheDocument();
    }

    const sample = rows[2];
    const tableRow = screen
      .getByRole('rowheader', { name: new RegExp(sample.fullLabel) })
      .closest('tr') as HTMLElement;
    const cells = within(tableRow).getAllByRole('cell');
    expect(cells[0].textContent).toBe(
      formatHours(sample[causes[0]] ?? 0, i18n.language),
    );
    expect(cells[cells.length - 1].textContent).toBe(
      formatHours(sample.total_hours, i18n.language),
    );
  });

  it('marks the partly observed buckets in the table and leaves the rest alone', async () => {
    const user = userEvent.setup();
    const causes = downtimeCauses(overview.fleet_series);
    const rows = downtimeRows(overview.fleet_series, causes, rowOptions());
    const partial = rows.filter((row) => row.bucketPartial);
    const complete = rows.filter((row) => !row.bucketPartial);
    expect(partial.length).toBeGreaterThan(0);
    expect(complete.length).toBeGreaterThan(0);

    render(<DowntimeByCauseWidget overview={overview} size={2} />);
    await user.click(screen.getByRole('button', { name: showData() }));

    const markerName = i18n.t('fleetMetrics.class.unobserved');
    const rowFor = (label: string): HTMLElement =>
      screen.getByRole('rowheader', { name: new RegExp(label) }).closest('tr') as HTMLElement;

    expect(within(rowFor(partial[0].fullLabel)).getByRole('img', { name: markerName })).toBeInTheDocument();
    expect(within(rowFor(complete[0].fullLabel)).queryByRole('img', { name: markerName })).toBeNull();
  });
});

describe('Cooling and eject — the per-printer switch', () => {
  it('starts on the model groups', () => {
    render(<CoolingAndEjectWidget overview={overview} />);

    const control = screen.getByRole('button', { name: i18n.t('fleetMetrics.widgets.perPrinter') });
    expect(control).toHaveAttribute('aria-pressed', 'false');
  });

  it('switches to the per-printer groups and names the printer', async () => {
    const user = userEvent.setup();
    render(<CoolingAndEjectWidget overview={overview} />);

    await user.click(screen.getByRole('button', { name: i18n.t('fleetMetrics.widgets.perPrinter') }));

    expect(
      screen.getByRole('button', { name: i18n.t('fleetMetrics.widgets.perPrinter') }),
    ).toHaveAttribute('aria-pressed', 'true');

    const printer = overview.matrix.printers.find(
      (entry) => entry.printer_id === HEALTHY_PRINTER_ID,
    );
    expect(printer).toBeDefined();
    const figure = screen.getByRole('figure', {
      name: i18n.t('fleetMetrics.sections.coolingAndEject'),
    });
    expect(within(figure).getByText(new RegExp(printer?.name ?? ''))).toBeInTheDocument();
  });

  it('heads its data table by the dimension actually in view', async () => {
    const user = userEvent.setup();
    render(<CoolingAndEjectWidget overview={overview} />);

    await user.click(screen.getByRole('button', { name: showData() }));
    expect(
      screen.getByRole('columnheader', { name: i18n.t('printers.model') }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: i18n.t('fleetMetrics.widgets.perPrinter') }));
    expect(
      screen.getByRole('columnheader', { name: i18n.t('common.printer') }),
    ).toBeInTheDocument();
  });

  it('keeps a deactivated printer out of the episode rows it never produced', () => {
    render(<CoolingAndEjectWidget overview={overview} />);

    const deactivated = overview.matrix.printers.find(
      (entry) => entry.printer_id === DEACTIVATED_PRINTER_ID,
    );
    const figure = screen.getByRole('figure', {
      name: i18n.t('fleetMetrics.sections.coolingAndEject'),
    });
    expect(within(figure).queryByText(new RegExp(deactivated?.name ?? 'never'))).toBeNull();
  });
});

describe('empty states', () => {
  const firstRun = makeFleetOverviewFirstRun();

  it('says so rather than drawing an empty axis, and hides the switch', () => {
    render(<StateOverTimeWidget overview={firstRun} size={4} />);

    expect(screen.getByRole('status')).toHaveTextContent(i18n.t('fleetMetrics.states.empty'));
    expect(screen.queryByRole('button', { name: showData() })).toBeNull();
    expect(
      screen.queryByRole('img', { name: i18n.t('fleetMetrics.sections.stateOverTime') }),
    ).toBeNull();
  });

  it('empties exactly the widgets whose own source is empty on a first run', () => {
    render(<FleetWidgets overview={firstRun} />);

    // Nothing was ever observed, so the three state- and print-derived widgets
    // are empty; the incident, episode and unit ledgers still have history.
    // Filtered by the message, because recharts mounts a live region of its
    // own for every plot it draws.
    const empties = screen
      .getAllByRole('status')
      .filter((node) => node.textContent === i18n.t('fleetMetrics.states.empty'));
    expect(empties).toHaveLength(3);
    expect(screen.getAllByRole('button', { name: showData() })).toHaveLength(3);
  });
});
