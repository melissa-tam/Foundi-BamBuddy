/**
 * Downtime by cause — where the fleet's lost hours went.
 *
 * ## The arithmetic
 *
 * The endpoint states downtime as AVERAGE CONCURRENT PRINTERS down. Multiplied
 * by the bucket's ELAPSED hours that is hours down — 0.5 printers down across a
 * 24 h day is 12 hours of lost machine time. (Strictly the product is a
 * printer-hour; the word never appears on the surface, the axis reads hours
 * down.) Elapsed and not the bucket's width, so today's half-finished day is
 * not reported as a quiet one, and the causes therefore sum exactly to the
 * total because the backend's own identity is Σ down-by-cause = down.
 *
 * ## Why one hue
 *
 * All twelve causes are the same fact — the printer cannot take work — so they
 * share the down hue and separate by VALUE, actionable causes first. Twelve
 * hues would claim the causes are twelve different kinds of thing, and the only
 * other hues this palette owns already mean printing, between prints and
 * maintenance. The bands are NOT additionally hatched: in a chart where every
 * band is down, the hatch would be constant and would only flatten the shade
 * ladder that carries the identity. The sparse hatch is kept for its one job —
 * marking a bucket the recorder only partly covered.
 *
 * The legend lists only the causes that actually occurred in the window, so a
 * clean month does not print a twelve-entry legend of things that did not
 * happen.
 */
import { useTranslation } from 'react-i18next';
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { ChartDataTable, type ChartDataColumn } from '../ChartDataTable';
import {
  ChartFrame,
  ChartLegend,
  PartialAwareBar,
  type ChartLegendEntry,
} from './ChartFrame';
import { AXIS_TICK_SIZE, CHART_HEIGHT } from './chartLayout';
import { downtimeCauses, downtimeRows, downtimeTotals, type DowntimeRowValues } from './rows';
import type { FleetOverview } from '../../../types/fleetMetrics';
import {
  CHART_AXIS_STROKE,
  CHART_GRID_DASH,
  CHART_GRID_STROKE,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  chartAxisTick,
} from '../../../utils/chartChrome';
import {
  causeLabelKey,
  downCauseColor,
  downCauseTextColor,
  formatHours,
  STATE_SUM_UNCERTAINTY,
} from '../../../utils/fleetMetrics';

export interface DowntimeByCauseWidgetProps {
  overview: FleetOverview;
  size: 1 | 2 | 4;
}

export function DowntimeByCauseWidget({ overview, size }: DowntimeByCauseWidgetProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const options = {
    bucket: overview.bucket,
    locale,
    todayLabel: t('fleetMetrics.units.today'),
  };

  const causes = downtimeCauses(overview.fleet_series);
  const rows = downtimeRows(overview.fleet_series, causes, options);
  const totals = downtimeTotals(overview.fleet_series, causes);

  const legend: ChartLegendEntry[] = causes.map((cause) => ({
    key: cause,
    label: t(causeLabelKey(cause)),
    color: downCauseColor(cause),
    textColor: downCauseTextColor(cause),
  }));

  const columns: ChartDataColumn<DowntimeRowValues>[] = [
    ...causes.map((cause) => ({
      key: cause,
      header: t(causeLabelKey(cause)),
      unit: t('fleetMetrics.units.hours'),
      format: (values: DowntimeRowValues) => formatHours(values[cause] ?? 0, locale),
    })),
    {
      key: 'total_hours',
      header: t('fleetMetrics.matrix.lens.hoursDown'),
      unit: t('fleetMetrics.units.hours'),
      format: (values: DowntimeRowValues) => formatHours(values.total_hours, locale),
    },
  ];

  return (
    <ChartFrame
      title={t('fleetMetrics.sections.downtimeByCause')}
      isEmpty={causes.length === 0 || rows.length === 0}
      legend={<ChartLegend entries={legend} />}
      chart={
        <ResponsiveContainer width="100%" height={CHART_HEIGHT[size]}>
          <BarChart data={rows} accessibilityLayer margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
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
              width={38}
              unit={t('fleetMetrics.units.hours')}
            />
            <Tooltip
              contentStyle={CHART_TOOLTIP_CONTENT_STYLE}
              labelStyle={CHART_TOOLTIP_LABEL_STYLE}
              formatter={(value) =>
                typeof value === 'number'
                  ? `${formatHours(value, locale)} ${t('fleetMetrics.units.hours')}`
                  : ''
              }
            />
            {causes.map((cause) => (
              <Bar
                key={cause}
                dataKey={cause}
                name={t(causeLabelKey(cause))}
                stackId="cause"
                fill={downCauseColor(cause)}
                // THE state-derived chart on this tab: its hours come from the
                // classified timeline, so a running bucket AND a recorder gap
                // both make the bar short of what the finished bucket will say.
                shape={(props) => (
                  <PartialAwareBar
                    {...props}
                    fill={downCauseColor(cause)}
                    uncertain={STATE_SUM_UNCERTAINTY}
                  />
                )}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      }
      table={
        <ChartDataTable<DowntimeRowValues>
          caption={t('fleetMetrics.sections.downtimeByCause')}
          rowHeader={t('fleetMetrics.widgets.bucketColumn')}
          columns={columns}
          rows={rows.map((row) => ({
            key: row.bucketStart,
            header: row.fullLabel,
            inProgress: row.bucketInProgress,
            partlyObserved: row.bucketPartlyObserved,
            values: row,
          }))}
          totals={{ key: 'window', header: t('fleetMetrics.matrix.columns.total'), values: totals }}
        />
      }
    />
  );
}

export default DowntimeByCauseWidget;
