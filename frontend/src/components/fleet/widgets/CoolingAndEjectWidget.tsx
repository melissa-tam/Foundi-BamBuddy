/**
 * Cooling and eject — how long the farm's own work between prints takes.
 *
 * The question it exists to answer is "is fan-only cooldown slower than hold +
 * fan", and the answer is two adjacent rows.
 *
 * ## Why a table-chart and not a grouped bar chart
 *
 * The whole comparison is at most about six groups: two printer models × two
 * cooldown modes, plus the eject rows. A grouped bar chart of six categories ×
 * two measures spends most of a half-width widget on axis furniture, needs its
 * category labels rotated or truncated to fit, and — the deciding point — has
 * nowhere to put the three numbers that stop a median being over-read: how many
 * episodes it is a median OF, how many ran over their expectation, and how many
 * never finished at all. A row per group puts the median bar, the p90 mark and
 * all three numbers on one line, at reading size, and the mode-to-mode
 * comparison becomes a vertical scan instead of a hunt across paired bars.
 *
 * It is drawn in HTML rather than SVG, so the figures are real text: the frame
 * therefore names it as a `<figure>` rather than hiding it behind `role="img"`.
 *
 * ## Statistics pass through
 *
 * Nothing here averages a median or blends two groups. `not_completed` is a
 * count beside the percentiles and never inside them — an eject the watchdog
 * stopped has no duration to contribute — and the over-expected share rides the
 * eject rows only, because a cooldown ends at a temperature and has nothing to
 * be late against.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../Button';
import { ChartDataTable, type ChartDataColumn } from '../ChartDataTable';
import { ChartFrame, ChartLegend, type ChartLegendEntry } from './ChartFrame';
import { NO_VALUE, cycleNotCompleted, cycleRows, type CycleRow } from './rows';
import type { FleetOverview } from '../../../types/fleetMetrics';
import {
  FLEET_GROUP_COLOR,
  FLEET_GROUP_TEXT,
  SECONDARY_TEXT_CLASS,
  causeLabelKey,
  formatCount,
  formatDuration,
  formatPercent,
} from '../../../utils/fleetMetrics';

/**
 * The eject purposes the backend mints, each with its own leaf. Any other
 * string falls through to itself — see `variantLabel`.
 */
const EJECT_PURPOSES: readonly string[] = ['production', 'fa', 'manual'];

/** The median bar takes the between-prints hue: this IS the between-prints work. */
const MEDIAN_COLOR = FLEET_GROUP_COLOR.cycle_overhead;
/** The slow tail is the same measure at a lower emphasis, so it takes the neutral. */
const P90_COLOR = FLEET_GROUP_COLOR.idle;

export interface CoolingAndEjectWidgetProps {
  overview: FleetOverview;
}

export function CoolingAndEjectWidget({ overview }: CoolingAndEjectWidgetProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const [perPrinter, setPerPrinter] = useState(false);

  const rows = cycleRows(overview.cycle, { perPrinter });
  const notCompleted = cycleNotCompleted(rows);

  const printerName = (printerId: number): string =>
    overview.matrix.printers.find((printer) => printer.printer_id === printerId)?.name ??
    t('fleetMetrics.matrix.deletedPrinter', { id: printerId });

  /**
   * A cooldown's variant is one of two measured modes; an eject's variant is
   * its PURPOSE, and the backend spells three (`production`, `fa`, `manual` —
   * `services/eject/remote.py` parses the job name into exactly those).
   *
   * Both vocabularies get real copy. The fall-back to the raw string stays for
   * a purpose a later build invents: printing `dry_run` is ugly but true, and
   * silently dropping a row the operator's episodes went into is not.
   */
  const variantLabel = (row: CycleRow): string | null => {
    if (row.variant === null) return null;
    if (row.kind === 'cooldown') {
      return row.variant === 'hold' || row.variant === 'fan_only'
        ? t(`fleetMetrics.widgets.variant.${row.variant}`)
        : row.variant;
    }
    return EJECT_PURPOSES.includes(row.variant)
      ? t(`fleetMetrics.widgets.ejectPurpose.${row.variant}`)
      : row.variant;
  };

  const rowLabel = (row: CycleRow): string => {
    const kind = t(causeLabelKey(row.kind === 'cooldown' ? 'cooling' : 'ejecting'));
    const subject =
      row.printerId === null ? (row.model ?? t('common.unknown')) : printerName(row.printerId);
    const variant = variantLabel(row);
    return [kind, subject, variant].filter((part) => part !== null && part !== '').join(' · ');
  };

  /** The bar scale: the slowest p90 in view, so every row is on one scale. */
  const scaleMax = rows.reduce(
    (max, row) => Math.max(max, row.p90S ?? 0, row.medianS ?? 0),
    0,
  );
  const share = (seconds: number | null): number =>
    scaleMax > 0 && seconds !== null ? Math.min(100, (seconds / scaleMax) * 100) : 0;

  const duration = (seconds: number | null): string =>
    seconds === null ? NO_VALUE : formatDuration(seconds, locale);

  const legend: ChartLegendEntry[] = [
    {
      key: 'median',
      label: t('fleetMetrics.widgets.median'),
      color: MEDIAN_COLOR,
      textColor: FLEET_GROUP_TEXT.cycle_overhead,
    },
    { key: 'p90', label: t('fleetMetrics.widgets.p90'), color: P90_COLOR, line: 'solid' as const },
  ];

  const columns: ChartDataColumn<CycleRow>[] = [
    {
      key: 'count',
      header: t('fleetMetrics.widgets.count'),
      format: (row) => formatCount(row.count, locale),
    },
    {
      key: 'median',
      header: t('fleetMetrics.widgets.median'),
      format: (row) => duration(row.medianS),
    },
    { key: 'p90', header: t('fleetMetrics.widgets.p90'), format: (row) => duration(row.p90S) },
    {
      key: 'over_expected',
      header: t('fleetMetrics.widgets.overExpected'),
      format: (row) =>
        row.overExpectedShare === null ? NO_VALUE : formatPercent(row.overExpectedShare, locale),
    },
    {
      key: 'not_completed',
      header: t('fleetMetrics.widgets.notCompleted'),
      format: (row) => formatCount(row.notCompleted, locale),
    },
  ];

  return (
    <ChartFrame
      title={t('fleetMetrics.sections.coolingAndEject')}
      isEmpty={rows.length === 0}
      graphic={false}
      legend={<ChartLegend entries={legend} />}
      controls={
        <Button
          variant="secondary"
          size="sm"
          aria-pressed={perPrinter}
          onClick={() => setPerPrinter((on) => !on)}
        >
          {t('fleetMetrics.widgets.perPrinter')}
        </Button>
      }
      footer={
        notCompleted > 0 ? (
          <p className={`text-xs ${SECONDARY_TEXT_CLASS}`}>
            {t('fleetMetrics.widgets.notCompleted')} {formatCount(notCompleted, locale)}
          </p>
        ) : undefined
      }
      chart={
        <ul className="space-y-2.5">
          {rows.map((row) => (
            <li key={row.id} className="space-y-1">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
                <span className="min-w-0 truncate text-xs text-white">{rowLabel(row)}</span>
                <span className={`text-xs tabular-nums ${SECONDARY_TEXT_CLASS}`}>
                  {duration(row.medianS)} · {t('fleetMetrics.widgets.p90')} {duration(row.p90S)} ·{' '}
                  {t('fleetMetrics.widgets.count')} {formatCount(row.count, locale)}
                  {row.overExpectedShare !== null && (
                    <>
                      {' · '}
                      {formatPercent(row.overExpectedShare, locale)}{' '}
                      {t('fleetMetrics.widgets.overExpected')}
                    </>
                  )}
                </span>
              </div>
              <div
                aria-hidden="true"
                className="relative h-2 w-full overflow-hidden rounded-sm bg-bambu-dark"
              >
                <div
                  className="h-2 rounded-sm"
                  style={{ width: `${share(row.medianS)}%`, backgroundColor: MEDIAN_COLOR }}
                />
                {row.p90S !== null && (
                  <div
                    className="absolute top-0 h-2 w-0.5"
                    style={{ left: `${share(row.p90S)}%`, backgroundColor: P90_COLOR }}
                  />
                )}
              </div>
            </li>
          ))}
        </ul>
      }
      table={
        <ChartDataTable<CycleRow>
          caption={t('fleetMetrics.sections.coolingAndEject')}
          rowHeader={perPrinter ? t('common.printer') : t('printers.model')}
          columns={columns}
          rows={rows.map((row) => ({ key: row.id, header: rowLabel(row), values: row }))}
        />
      }
    />
  );
}

export default CoolingAndEjectWidget;
