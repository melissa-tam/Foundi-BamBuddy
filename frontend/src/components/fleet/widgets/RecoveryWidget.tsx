/**
 * Recovery — when faults arrived, how long they stood, and who had to touch the
 * printer.
 *
 * The bars are incidents OPENED per bucket: an arrival rate, not a standing
 * count, so a single week-long outage is one bar and not seven.
 *
 * **"Fault hours open" is not "hours down".** They answer different questions
 * and the plan keeps them apart everywhere: hours down is time a printer could
 * not take work, measured against the classified timeline; fault hours open is
 * how long an incident ROW stood, clipped to the window. A printer can be
 * printing under an open hold, and an offline printer with no incident is down
 * with no fault hours at all. The two labels never merge.
 *
 * **Time to recover is per KIND, never blended.** The endpoint returns a median
 * per kind, and a median of medians is not a median; the headline therefore
 * names the kind it belongs to (the one that arrived most often) and every
 * other kind's median is in the data table, with the full list on the hint.
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
  HeadlineFigures,
  PartialAwareBar,
  type ChartLegendEntry,
} from './ChartFrame';
import { FleetChartTooltip } from './FleetChartTooltip';
import { AXIS_TICK_SIZE, CHART_HEIGHT } from './chartLayout';
import {
  NO_VALUE,
  recoveryHeadline,
  recoveryKinds,
  recoveryRows,
  recoveryTotals,
  type RecoveryRowValues,
} from './rows';
import type { FleetOverview } from '../../../types/fleetMetrics';
import {
  CHART_AXIS_STROKE,
  CHART_GRID_DASH,
  CHART_GRID_STROKE,
  chartAxisTick,
} from '../../../utils/chartChrome';
import {
  formatCount,
  formatDuration,
  formatHours,
  formatPercent,
  incidentKindColor,
  incidentKindLabelKey,
  incidentKindTextColor,
  SUM_UNCERTAINTY,
} from '../../../utils/fleetMetrics';

export interface RecoveryWidgetProps {
  overview: FleetOverview;
  size: 1 | 2 | 4;
}

export function RecoveryWidget({ overview, size }: RecoveryWidgetProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const options = {
    bucket: overview.bucket,
    locale,
    todayLabel: t('fleetMetrics.units.today'),
  };

  const { recovery } = overview;
  const kinds = recoveryKinds(recovery.series);
  const rows = recoveryRows(recovery.series, kinds, options);
  const totals = recoveryTotals(recovery.series, kinds);
  const headline = recoveryHeadline(recovery);

  /** Every kind's own median, for the hint behind the headline figure. */
  const recoveryByKind = recovery.time_to_recover
    .filter((entry) => entry.median_recover_s !== null)
    .map(
      (entry) =>
        `${t(incidentKindLabelKey(entry.kind))}: ${formatDuration(entry.median_recover_s ?? 0, locale)}`,
    )
    .join(' · ');

  const legend: ChartLegendEntry[] = kinds.map((kind) => ({
    key: kind,
    label: t(incidentKindLabelKey(kind)),
    color: incidentKindColor(kind),
    textColor: incidentKindTextColor(kind),
  }));

  const columns: ChartDataColumn<RecoveryRowValues>[] = [
    ...kinds.map((kind) => ({
      key: kind,
      header: t(incidentKindLabelKey(kind)),
      format: (values: RecoveryRowValues) => formatCount(values[kind] ?? 0, locale),
    })),
    {
      key: 'opened',
      header: t('fleetMetrics.widgets.opened'),
      format: (values: RecoveryRowValues) => formatCount(values.opened, locale),
    },
  ];

  return (
    <ChartFrame
      title={t('fleetMetrics.sections.recovery')}
      isEmpty={kinds.length === 0 || rows.length === 0}
      legend={<ChartLegend entries={legend} />}
      headline={
        <HeadlineFigures
          figures={[
            {
              key: 'time_to_recover',
              label: t('fleetMetrics.widgets.timeToRecover'),
              value:
                headline.leadRecovery?.median_recover_s == null
                  ? NO_VALUE
                  : formatDuration(headline.leadRecovery.median_recover_s, locale),
              detail:
                headline.leadRecovery === null
                  ? undefined
                  : t(incidentKindLabelKey(headline.leadRecovery.kind)),
              hint: recoveryByKind === '' ? undefined : recoveryByKind,
            },
            {
              key: 'cleared_no_person',
              label: t('fleetMetrics.widgets.clearedNoPerson'),
              value:
                headline.clearedNoPersonShare === null
                  ? NO_VALUE
                  : formatPercent(headline.clearedNoPersonShare, locale),
            },
            {
              key: 'fault_hours_open',
              label: t('fleetMetrics.widgets.faultHoursOpen'),
              value: `${formatHours(headline.faultOpenHours, locale)} ${t('fleetMetrics.units.hours')}`,
            },
          ]}
        />
      }
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
              width={30}
              allowDecimals={false}
            />
            <Tooltip
              content={(props) => (
                <FleetChartTooltip
                  active={props.active}
                  payload={props.payload}
                  formatValue={(value) => formatCount(value, locale)}
                />
              )}
            />
            {kinds.map((kind) => (
              <Bar
                key={kind}
                dataKey={kind}
                name={t(incidentKindLabelKey(kind))}
                stackId="kind"
                fill={incidentKindColor(kind)}
                // The incident ledger is complete for its own history — a
                // recorder gap never hid a fault row — so only a RUNNING
                // bucket makes this arrival count short.
                shape={(props) => (
                  <PartialAwareBar
                    {...props}
                    fill={incidentKindColor(kind)}
                    uncertain={SUM_UNCERTAINTY}
                  />
                )}
                isAnimationActive={false}
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      }
      table={
        <ChartDataTable<RecoveryRowValues>
          caption={t('fleetMetrics.sections.recovery')}
          rowHeader={t('fleetMetrics.widgets.bucketColumn')}
          columns={columns}
          rows={rows.map((row) => ({
            key: row.bucketStart,
            header: row.fullLabel,
            inProgress: row.bucketInProgress,
            values: row,
          }))}
          totals={{ key: 'window', header: t('fleetMetrics.matrix.columns.total'), values: totals }}
        />
      }
    />
  );
}

export default RecoveryWidget;
