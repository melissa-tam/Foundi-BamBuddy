/**
 * Prints per day — what the farm actually made, split by outcome.
 *
 * The bars are counts of prints; the line is prints per printer per day. Both
 * are prints, so both sit on ONE axis: a second y-scale would let the line be
 * dragged anywhere against the bars and is the single most common way a chart
 * lies. A fleet of twelve makes the line sit low, which is the honest picture —
 * the reader is asking whether each machine is pulling its weight, not whether
 * the line is tall.
 *
 * Success % is the widget's headline rather than a band, because it is a ratio
 * of two of the bands and stacking it would double-count them. Cancelled prints
 * are excluded from its denominator (an operator stop is neither a quality
 * success nor a quality failure) — that is the backend's own definition, and it
 * is not recomputed here.
 */
import { useTranslation } from 'react-i18next';
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
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
import { NO_VALUE, printsRows, printsTotals, type PrintsRowValues } from './rows';
import type { FleetOverview, PrintOutcome } from '../../../types/fleetMetrics';
import {
  CHART_AXIS_STROKE,
  CHART_GRID_DASH,
  CHART_GRID_STROKE,
  CHART_MUTED_TEXT,
  chartAxisTick,
} from '../../../utils/chartChrome';
import {
  OUTCOME_COLOR,
  OUTCOME_LABEL_KEY,
  OUTCOME_ORDER,
  OUTCOME_PATTERN,
  OUTCOME_TEXT,
  formatCount,
  formatPercent,
  formatPrinters,
  SUM_UNCERTAINTY,
} from '../../../utils/fleetMetrics';

export interface PrintsPerDayWidgetProps {
  overview: FleetOverview;
  size: 1 | 2 | 4;
}

export function PrintsPerDayWidget({ overview, size }: PrintsPerDayWidgetProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const options = {
    bucket: overview.bucket,
    locale,
    todayLabel: t('fleetMetrics.units.today'),
  };

  const rows = printsRows(overview.throughput, options);
  const totals = printsTotals(overview.throughput);
  const successPct = overview.throughput.totals.success_pct;

  const legend: ChartLegendEntry[] = [
    ...OUTCOME_ORDER.map((outcome) => ({
      key: outcome,
      label: t(OUTCOME_LABEL_KEY[outcome]),
      color: OUTCOME_COLOR[outcome],
      textColor: OUTCOME_TEXT[outcome],
      pattern: OUTCOME_PATTERN[outcome],
    })),
    {
      key: 'per_printer',
      label: t('fleetMetrics.summary.rows.prints_per_printer_per_day'),
      color: CHART_MUTED_TEXT,
      line: 'solid' as const,
    },
  ];

  const columns: ChartDataColumn<PrintsRowValues>[] = [
    ...OUTCOME_ORDER.map((outcome) => ({
      key: outcome,
      header: t(OUTCOME_LABEL_KEY[outcome]),
      format: (values: PrintsRowValues) => formatCount(values[outcome], locale),
    })),
    {
      key: 'total',
      header: t('fleetMetrics.matrix.columns.total'),
      format: (values: PrintsRowValues) => formatCount(values.total, locale),
    },
    {
      key: 'per_printer',
      header: t('fleetMetrics.summary.rows.prints_per_printer_per_day'),
      format: (values: PrintsRowValues) =>
        values.per_printer === null ? NO_VALUE : formatPrinters(values.per_printer, locale),
    },
  ];

  return (
    <ChartFrame
      title={t('fleetMetrics.sections.printsPerDay')}
      isEmpty={rows.length === 0 || totals.total === 0}
      legend={<ChartLegend entries={legend} />}
      headline={
        <HeadlineFigures
          figures={[
            {
              key: 'success',
              label: t('fleetMetrics.widgets.successRate'),
              value: successPct === null ? NO_VALUE : formatPercent(successPct, locale),
            },
            {
              key: 'total',
              label: t('fleetMetrics.matrix.columns.total'),
              value: formatCount(totals.total, locale),
            },
          ]}
        />
      }
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
              allowDecimals={false}
            />
            <Tooltip
              content={(props) => (
                <FleetChartTooltip
                  active={props.active}
                  payload={props.payload}
                  formatValue={(value) => formatCount(value, locale)}
                  patternFor={(key) => OUTCOME_PATTERN[key as PrintOutcome]}
                />
              )}
            />
            {OUTCOME_ORDER.map((outcome) => (
              <Bar
                key={outcome}
                dataKey={outcome}
                name={t(OUTCOME_LABEL_KEY[outcome])}
                stackId="outcome"
                fill={OUTCOME_COLOR[outcome]}
                // The print log is complete for its own history, so only a
                // RUNNING bucket makes this bar short. A recorder gap says
                // nothing about whether a print finished.
                shape={(props) => (
                  <PartialAwareBar
                    {...props}
                    fill={OUTCOME_COLOR[outcome]}
                    pattern={OUTCOME_PATTERN[outcome]}
                    uncertain={SUM_UNCERTAINTY}
                  />
                )}
                isAnimationActive={false}
              />
            ))}
            <Line
              type="linear"
              dataKey="per_printer"
              name={t('fleetMetrics.summary.rows.prints_per_printer_per_day')}
              stroke={CHART_MUTED_TEXT}
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
        <ChartDataTable<PrintsRowValues>
          caption={t('fleetMetrics.sections.printsPerDay')}
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

export default PrintsPerDayWidget;
