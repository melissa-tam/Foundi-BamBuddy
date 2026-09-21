/**
 * Printers by state over time — the tab's one picture of the whole fleet.
 *
 * Read as: at any moment in this bucket, on average this many printers were
 * printing, between prints, idle, in maintenance, down. The unit is AVERAGE
 * CONCURRENT PRINTERS, the same unit the live counts are in, which is why the
 * dashed "in fleet" line is a ceiling the stack can be compared against rather
 * than a separate scale. There is exactly one y-axis here and everywhere on
 * this tab.
 *
 * ## The pattern channel, in recharts 3
 *
 * A band carries three channels: hue, a luminance step, and a pattern. Recharts
 * paints a band with a single `fill`, and the shared `<pattern>` defs are
 * transparent (they stroke, they do not fill), so colour and texture cannot
 * both ride one `fill`.
 *
 * So the stack is drawn TWICE. The first stack (`stackId="state"`) is the
 * colours. The second (`stackId="state-pattern"`) carries the identical
 * dataKeys in the identical order — which makes its geometry identical, since a
 * stack offset is a function of the values and their order — and fills only the
 * two groups that own a pattern, `none` for the rest. It is excluded from the
 * legend and the tooltip, so it is paint and nothing else.
 *
 * The alternative was a second family of `<pattern>` defs with a colour rect
 * behind the strokes. That would re-spell the 6 px tile, the 2 px stroke and
 * the 45° rotation that `FleetPatternDefs` already owns, once per band colour,
 * and the two copies would drift the first time a stroke width changed.
 *
 * ## No data is a gap, never a zero
 *
 * `unobserved` rides ON TOP of the five state bands as a sparse-hatched,
 * colourless band: in a bucket the recorder never covered, the state bands are
 * `null` (recharts draws a hole) and the hatch fills the bucket to the roster
 * line. A fault the incident ledger proves still draws its red band there —
 * that evidence is durable and does not depend on the recorder.
 */
import { useTranslation } from 'react-i18next';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { ChartDataTable, type ChartDataColumn } from '../ChartDataTable';
import { ChartFrame, ChartLegend, type ChartLegendEntry } from './ChartFrame';
import { FleetChartTooltip } from './FleetChartTooltip';
import { AXIS_TICK_SIZE, CHART_HEIGHT } from './chartLayout';
import { NO_VALUE, stateHasData, stateRows, stateTotals, type StateRowValues } from './rows';
import type { FleetOverview } from '../../../types/fleetMetrics';
import {
  CHART_AXIS_STROKE,
  CHART_GRID_DASH,
  CHART_GRID_STROKE,
  CHART_MUTED_TEXT,
  chartAxisTick,
} from '../../../utils/chartChrome';
import {
  CHART_STACK_GROUPS,
  type ChartedGroup,
  FLEET_ABSENCE_COLOR,
  FLEET_ABSENCE_TEXT,
  FLEET_GROUP_COLOR,
  FLEET_GROUP_TEXT,
  GROUP_PATTERN,
  formatPrinters,
  groupLabelKey,
  patternFill,
} from '../../../utils/fleetMetrics';

export interface StateOverTimeWidgetProps {
  overview: FleetOverview;
  size: 1 | 2 | 4;
}

export function StateOverTimeWidget({ overview, size }: StateOverTimeWidgetProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const options = {
    bucket: overview.bucket,
    locale,
    todayLabel: t('fleetMetrics.units.today'),
  };

  const rows = stateRows(overview.fleet_series, options);
  const totals = stateTotals(overview.fleet_series);
  const printers = (value: number | null): string =>
    value === null ? NO_VALUE : formatPrinters(value, locale);

  const legend: ChartLegendEntry[] = [
    ...CHART_STACK_GROUPS.map((group) => ({
      key: group,
      label: t(groupLabelKey(group)),
      color: FLEET_GROUP_COLOR[group],
      textColor: FLEET_GROUP_TEXT[group],
      pattern: GROUP_PATTERN[group],
    })),
    {
      key: 'unobserved',
      label: t('fleetMetrics.class.unobserved'),
      color: FLEET_ABSENCE_COLOR.unobserved,
      textColor: FLEET_ABSENCE_TEXT.unobserved,
      pattern: GROUP_PATTERN.unobserved,
    },
    {
      key: 'in_fleet',
      label: t('fleetMetrics.widgets.inFleetLine'),
      color: CHART_MUTED_TEXT,
      line: 'dashed' as const,
    },
    {
      key: 'peak_down',
      label: t('fleetMetrics.widgets.peakDownLine'),
      color: FLEET_GROUP_COLOR.down,
      line: 'solid' as const,
    },
  ];

  const columns: ChartDataColumn<StateRowValues>[] = [
    ...CHART_STACK_GROUPS.map((group) => ({
      key: group,
      header: t(groupLabelKey(group)),
      format: (values: StateRowValues) => printers(values[group]),
    })),
    {
      key: 'unobserved',
      header: t('fleetMetrics.class.unobserved'),
      format: (values: StateRowValues) => printers(values.unobserved),
    },
    {
      key: 'peak_down',
      header: t('fleetMetrics.widgets.peakDownLine'),
      format: (values: StateRowValues) => printers(values.peak_down),
    },
    {
      key: 'in_fleet',
      header: t('fleetMetrics.widgets.inFleetLine'),
      format: (values: StateRowValues) => printers(values.in_fleet),
    },
  ];

  return (
    <ChartFrame
      title={t('fleetMetrics.sections.stateOverTime')}
      isEmpty={!stateHasData(rows)}
      legend={<ChartLegend entries={legend} />}
      chart={
        <ResponsiveContainer width="100%" height={CHART_HEIGHT[size]}>
          <ComposedChart data={rows} accessibilityLayer margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray={CHART_GRID_DASH} stroke={CHART_GRID_STROKE} />
            <XAxis
              dataKey="axisLabel"
              stroke={CHART_AXIS_STROKE}
              tick={chartAxisTick(AXIS_TICK_SIZE[size])}
              interval="preserveStartEnd"
              minTickGap={8}
            />
            <YAxis
              stroke={CHART_AXIS_STROKE}
              tick={chartAxisTick(AXIS_TICK_SIZE[size])}
              width={34}
              allowDecimals
            />
            <Tooltip
              content={(props) => (
                <FleetChartTooltip
                  active={props.active}
                  payload={props.payload}
                  formatValue={(value) => printers(value)}
                  patternFor={(key) => GROUP_PATTERN[key as ChartedGroup]}
                />
              )}
            />

            {/* Channel one and two: hue and the luminance ladder, bottom to top. */}
            {CHART_STACK_GROUPS.map((group) => (
              <Area
                key={group}
                type="linear"
                dataKey={group}
                name={t(groupLabelKey(group))}
                stackId="state"
                stroke="none"
                fill={FLEET_GROUP_COLOR[group]}
                fillOpacity={1}
                connectNulls={false}
                activeDot={false}
                isAnimationActive={false}
              />
            ))}
            {/* The recorder's blind spot: texture only, so it can never be read
                as one of the five states. */}
            <Area
              type="linear"
              dataKey="unobserved"
              name={t('fleetMetrics.class.unobserved')}
              stackId="state"
              stroke="none"
              fill={patternFill('sparse')}
              fillOpacity={1}
              connectNulls={false}
              activeDot={false}
              isAnimationActive={false}
            />

            {/* Channel three, on a twin stack: identical dataKeys in identical
                order give identical geometry, so this lands exactly on the
                colour bands it textures. */}
            {CHART_STACK_GROUPS.map((group) => {
              const pattern = GROUP_PATTERN[group];
              return (
                <Area
                  key={`pattern-${group}`}
                  type="linear"
                  dataKey={group}
                  stackId="state-pattern"
                  stroke="none"
                  fill={pattern ? patternFill(pattern) : 'none'}
                  fillOpacity={1}
                  connectNulls={false}
                  activeDot={false}
                  isAnimationActive={false}
                  legendType="none"
                  tooltipType="none"
                />
              );
            })}

            {/* The roster ceiling. It moves with activations and deletions, so
                it is a series and not a fixed reference line — and it BREAKS
                over a bucket the recorder never reached, where the roster is
                unknown rather than empty. `connectNulls={false}` is stated
                rather than left to the default: a line drawn straight across
                that gap would be the chart asserting the ceiling it does not
                have. */}
            <Line
              type="stepAfter"
              dataKey="in_fleet"
              name={t('fleetMetrics.widgets.inFleetLine')}
              stroke={CHART_MUTED_TEXT}
              strokeDasharray="4 3"
              strokeWidth={1.5}
              dot={false}
              activeDot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            <Line
              type="stepAfter"
              dataKey="peak_down"
              name={t('fleetMetrics.widgets.peakDownLine')}
              stroke={FLEET_GROUP_COLOR.down}
              strokeWidth={2}
              dot={false}
              activeDot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      }
      table={
        <ChartDataTable<StateRowValues>
          caption={t('fleetMetrics.sections.stateOverTime')}
          rowHeader={t('fleetMetrics.widgets.bucketColumn')}
          columns={columns}
          rows={rows.map((row) => ({
            key: row.bucketStart,
            header: row.fullLabel,
            // AVERAGES, not sums: every figure here already divides by the
            // bucket's ELAPSED seconds, so a bucket that is still running is
            // not understated and carries no in-progress marker. A recorder
            // gap is a different matter — these figures are state-derived.
            partlyObserved: row.bucketPartlyObserved,
            values: row,
          }))}
          totals={{
            key: 'window',
            header: t('fleetMetrics.matrix.columns.average'),
            values: totals,
          }}
        />
      }
    />
  );
}

export default StateOverTimeWidget;
