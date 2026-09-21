/**
 * Parts by SKU — sellable units out of the farm, not prints off a plate.
 *
 * The top five SKUs by WINDOW total get their own band and everything else
 * folds into "other". Five because this widget defaults to half the grid and
 * its legend sits under the plot: six entries stay on two readable lines at the
 * 1280 layout with the sidebar expanded, and a sixth band would also run past
 * the colours this palette owns. Ranking on the window rather than per bucket
 * keeps a band's colour and position fixed for the whole chart — a series that
 * changed identity mid-axis is unreadable.
 *
 * The "other" band is the bucket's own unit total MINUS the named SKUs, never a
 * sum of the leftovers, so the bands always add up to the figure the table
 * prints.
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
import { partsRows, partsTotals, skuSeries, type PartsRowValues, type SkuSeries } from './rows';
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
  SUM_UNCERTAINTY,
  formatCount,
  skuBandColor,
  skuBandText,
} from '../../../utils/fleetMetrics';

export interface PartsBySkuWidgetProps {
  overview: FleetOverview;
  size: 1 | 2 | 4;
}

export function PartsBySkuWidget({ overview, size }: PartsBySkuWidgetProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const options = {
    bucket: overview.bucket,
    locale,
    todayLabel: t('fleetMetrics.units.today'),
  };

  const series = skuSeries(overview.units);
  const rows = partsRows(overview.units, series, options);
  const totals = partsTotals(overview.units, series);

  const seriesLabel = (entry: SkuSeries): string =>
    entry.sku ?? t('fleetMetrics.widgets.otherSku');

  const legend: ChartLegendEntry[] = series.map((entry, index) => ({
    key: entry.key,
    label: seriesLabel(entry),
    color: skuBandColor(index, entry.sku === null),
    textColor: skuBandText(index, entry.sku === null),
  }));

  const columns: ChartDataColumn<PartsRowValues>[] = [
    ...series.map((entry) => ({
      key: entry.key,
      header: seriesLabel(entry),
      format: (values: PartsRowValues) => formatCount(values[entry.key] ?? 0, locale),
    })),
    {
      key: 'units',
      header: t('fleetMetrics.widgets.units'),
      format: (values: PartsRowValues) => formatCount(values.units, locale),
    },
  ];

  return (
    <ChartFrame
      title={t('fleetMetrics.sections.partsBySku')}
      isEmpty={series.length === 0 || rows.length === 0}
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
              width={34}
              allowDecimals={false}
            />
            <Tooltip
              contentStyle={CHART_TOOLTIP_CONTENT_STYLE}
              labelStyle={CHART_TOOLTIP_LABEL_STYLE}
              formatter={(value) => (typeof value === 'number' ? formatCount(value, locale) : '')}
            />
            {series.map((entry, index) => {
              const color = skuBandColor(index, entry.sku === null);
              return (
                <Bar
                  key={entry.key}
                  dataKey={entry.key}
                  name={seriesLabel(entry)}
                  stackId="sku"
                  fill={color}
                  // Units come from completed queue plates — a complete ledger.
                  // Only a running bucket understates them.
                  shape={(props) => (
                    <PartialAwareBar {...props} fill={color} uncertain={SUM_UNCERTAINTY} />
                  )}
                  isAnimationActive={false}
                />
              );
            })}
          </BarChart>
        </ResponsiveContainer>
      }
      table={
        <ChartDataTable<PartsRowValues>
          caption={t('fleetMetrics.sections.partsBySku')}
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

export default PartsBySkuWidget;
