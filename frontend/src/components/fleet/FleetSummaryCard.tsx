/**
 * FleetSummaryCard — the Fleet tab's fixed hero.
 *
 * Deliberately NOT a `Dashboard` widget: it answers the two questions an
 * operator opens this tab with ("is anything down right now" and "how did the
 * fleet do this period"), and a hero that can be dragged away or hidden is a
 * hero the next operator cannot find.
 *
 * ## One table, five columns
 *
 * The historic-twin rule IS the layout: row label · **Now** · the period
 * average · trend · change. A state row shows the SAME unit in both figure
 * columns — printers now beside average printers over the period — so "2 down
 * now" sits next to "2.1 down on average, peak 5" and the two can be compared
 * without converting anything. A rate row (prints per day, uptime) has no
 * instantaneous value, so its Now cell is blank rather than filled with a
 * number that would mean something different.
 *
 * It is a REAL `<table>`, not a grid of divs: every figure has a row header and
 * a column header, which is what lets a screen-reader user hear "Down, Last 30
 * days, 2.1" instead of a loose number.
 *
 * ## What this component is not allowed to do
 *
 * **It never computes a period figure.** Every one comes from
 * `overview.summary.rows` by key, formatted through
 * `utils/fleetMetrics.SUMMARY_ROW_FORMAT`. A figure derived here would be a
 * second definition of an average the backend already states, and the two would
 * disagree the first time the classifier changed. The one derivation it does
 * make is the "In fleet" Now count, and it makes it with the backend's own
 * identity, cited at the call site.
 *
 * ## Two queries, one card
 *
 * `status` and `overview` arrive separately and the card is built to render
 * with either one missing: Now fills the moment `/status` answers, and the
 * period columns fill later. That is the whole reason the Fleet tab runs two
 * queries instead of one.
 *
 * The footer is the honesty surface. A lights-out farm reading "0 down" from a
 * `/status` that stopped answering forty minutes ago is the most dangerous
 * thing this tab could show, so the footer states the age of the last GOOD
 * response and turns `status-warning` — with an icon, never colour alone —
 * once it passes 90 s. Its clock ticks on an interval of its own, because an
 * age that only advances when a refetch succeeds cannot report a refetch that
 * has stopped succeeding.
 */
import { useEffect, useId, useState } from 'react';
import { AlertTriangle, ArrowDown, ArrowUp, Minus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Card, CardContent } from '../Card';
import { InfoHint } from '../ui/InfoHint';
import { Sparkline } from './Sparkline';
import { TIMEFRAME_PERIOD_LABEL_KEY } from '../../hooks/useFleetMetrics';
import {
  DOWN_CAUSE_ORDER,
  SECONDARY_TEXT_CLASS,
  SUMMARY_ROW_FORMAT,
  SUMMARY_ROW_HINT_KEY,
  SUMMARY_ROW_LABEL_KEY,
  classLabelKey,
  formatCount,
  formatDuration,
  formatPercent,
  formatPoints,
  formatPrinters,
  readChange,
  type ChangeReading,
} from '../../utils/fleetMetrics';
import type {
  FleetGroup,
  FleetOverview,
  FleetStatus,
  PrinterStatus,
  SummaryRow,
  SummaryRowKey,
} from '../../types/fleetMetrics';
import type { TimeframeState } from '../../utils/timeframe';

/** The formats `SUMMARY_ROW_FORMAT` assigns a row. */
type RowFormat = (typeof SUMMARY_ROW_FORMAT)[SummaryRowKey];

/**
 * Summary row → the live class GROUP whose printer count is that row's "Now".
 *
 * The pairing is what makes the two figure columns comparable: `avg_down` is
 * average concurrent printers down, and `counts_by_group.down` is printers down
 * at this instant. A row absent from this map has no instantaneous twin (a rate
 * per day, a percentage) and leaves Now blank.
 *
 * Lives here rather than in `utils/fleetMetrics` only because nothing else
 * needs it yet — it is a candidate to move the moment a second surface pairs a
 * summary row with a live count.
 */
const NOW_GROUP: Partial<Record<SummaryRowKey, FleetGroup>> = {
  avg_printing: 'printing',
  avg_cycle_overhead: 'cycle_overhead',
  avg_idle: 'idle',
  avg_down: 'down',
  avg_planned: 'planned',
};

/**
 * The rows the card can render from `/status` ALONE, in the order the wireframe
 * reads them.
 *
 * Without this the card would have no rows at all until the history sweep
 * answered — the row SET came from the overview — and "is anything down right
 * now" would wait on the slow query after all, which is the one thing the
 * two-query split exists to prevent. Once `/overview` lands, the server's own
 * row list takes over completely; this is a floor, never a merge.
 *
 * Derived from `NOW_GROUP` rather than re-typed, so a row can never appear here
 * without the live count that gives it a Now column.
 */
const LIVE_ROW_ORDER: SummaryRowKey[] = [
  ...(Object.keys(NOW_GROUP) as SummaryRowKey[]),
  'printers_in_fleet',
];

/** Rendered as a sub-line under Down, never as a row of its own. */
const PEAK_ROW: SummaryRowKey = 'peak_down';
const PEAK_HOST_ROW: SummaryRowKey = 'avg_down';

/**
 * Decimals each format is COMPARED on, so an arrow never appears beside two
 * numbers a reader can see are identical. Ratios compare at two decimals
 * because they are displayed — and changed — in whole percentage points.
 */
const CHANGE_PRECISION: Record<RowFormat, number> = {
  printers: 1,
  count: 0,
  ratio: 2,
};

/** Past this age the last good `/status` is reported as stale, not as fact. */
const STALE_AFTER_MS = 90_000;
/** The footer's own clock. An external clock, which is what earns the effect. */
const CLOCK_TICK_MS = 1_000;
/** Down-now rows shown before the overflow count takes over. */
const MAX_DOWN_ROWS = 3;

/** Does this row have an instantaneous twin at all? Independent of any data. */
function hasNowColumn(key: SummaryRowKey): boolean {
  return key === 'printers_in_fleet' || NOW_GROUP[key] !== undefined;
}

/** The live printer count that pairs with a row, or null when it has none. */
function nowValue(key: SummaryRowKey, status: FleetStatus | undefined): number | null {
  if (status === undefined) return null;
  if (key === 'printers_in_fleet') {
    // The backend's own identity (`services/fleet_metrics` §Identities):
    // printers_in_fleet = printers_known − out_of_fleet − not_recorded.
    // Spelled out rather than subtracted from a total the response does not
    // carry, so the live count and the period average mean the same thing.
    const counts = status.counts_by_group;
    return status.printers.length - (counts.out_of_fleet ?? 0) - (counts.not_recorded ?? 0);
  }
  const group = NOW_GROUP[key];
  if (group === undefined) return null;
  return status.counts_by_group[group] ?? 0;
}

function formatFigure(value: number, format: RowFormat, locale: string): string {
  switch (format) {
    case 'ratio':
      return formatPercent(value, locale);
    case 'count':
      return formatCount(value, locale);
    default:
      return formatPrinters(value, locale);
  }
}

/**
 * The magnitude of a change, in the row's own unit. A ratio moves in percentage
 * POINTS — "uptime fell 2 pt" — because a percentage of a percentage is a
 * different claim and nobody reads it as the one intended.
 */
function formatMagnitude(
  magnitude: number,
  format: RowFormat,
  locale: string,
  pointsSymbol: string,
): string {
  switch (format) {
    case 'ratio':
      return `${formatPoints(magnitude, locale)} ${pointsSymbol}`;
    case 'count':
      return formatCount(magnitude, locale);
    default:
      return formatPrinters(magnitude, locale);
  }
}

/**
 * A site CALENDAR DATE (`YYYY-MM-DD`, already resolved by the server) spelled
 * the way the operator's locale spells one. Parsed AND formatted in UTC, so the
 * browser's own zone can never shift the server's date by a day.
 */
function formatSiteDate(date: string, locale: string): string {
  const parsed = Date.parse(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed)) return date;
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(parsed));
}

/** `HH:MM` on the READER's clock — the footer states when their page last updated. */
function formatClock(epochMs: number, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(epochMs));
}

/**
 * How long a printer has been in its current class, measured against the
 * SERVER's own "now" rather than the browser's: both instants come from the
 * same response, so the duration is exact and does not drift with a page that
 * has been open for an hour.
 */
function sinceSeconds(printer: PrinterStatus, generatedAt: string): number | null {
  if (printer.since === null) return null;
  const since = Date.parse(`${printer.since}Z`);
  const now = Date.parse(`${generatedAt}Z`);
  if (Number.isNaN(since) || Number.isNaN(now)) return null;
  return Math.max(0, (now - since) / 1000);
}

/**
 * Printers down right now, ACTIONABLE FIRST (`DOWN_CAUSE_ORDER`): a plate
 * nobody cleared or a missing USB drive is fixed by whoever is already standing
 * there, an equipment fault is not, so the first row is the one worth walking
 * over for. Ties break on name, so the list never reshuffles between polls.
 */
function downNowPrinters(status: FleetStatus | undefined): PrinterStatus[] {
  if (status === undefined) return [];
  const rank = (printer: PrinterStatus): number => {
    const index = printer.cause === null ? -1 : DOWN_CAUSE_ORDER.indexOf(printer.cause);
    return index === -1 ? DOWN_CAUSE_ORDER.length : index;
  };
  return status.printers
    .filter((printer) => printer.group === 'down')
    .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
}

interface ChangeCellProps {
  change: ChangeReading | null;
  format: RowFormat;
  /** The period the comparison is against, for the spelled-out sentence. */
  periodLabel: string;
}

/**
 * The change against the previous window: a direction arrow and a magnitude, in
 * NEUTRAL text.
 *
 * No green/red. More prints is good and more downtime is not, and a single
 * colour rule cannot tell those apart — so the arrow says which way, the row
 * label says what moved, and the reader draws the conclusion.
 *
 * The visible glyphs are `aria-hidden` and the cell carries the spelled-out
 * sentence as its accessible name, because "▲ 0.4" announced literally is not
 * a statement.
 */
function ChangeCell({ change, format, periodLabel }: ChangeCellProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;

  if (change === null) {
    // No previous window to compare against — a different claim from "no
    // change", so it is never rendered as a zero.
    return (
      <span role="img" aria-label={t('common.noData')} className={SECONDARY_TEXT_CLASS}>
        <span aria-hidden="true">—</span>
      </span>
    );
  }

  const value = formatMagnitude(change.magnitude, format, locale, t('fleetMetrics.units.points'));
  const Icon =
    change.direction === 'up' ? ArrowUp : change.direction === 'down' ? ArrowDown : Minus;

  return (
    <span
      role="img"
      aria-label={t(change.labelKey, { value, period: periodLabel })}
      className={`inline-flex items-center justify-end gap-1 whitespace-nowrap ${SECONDARY_TEXT_CLASS}`}
    >
      <Icon className="w-3.5 h-3.5" aria-hidden="true" />
      {change.direction !== 'none' && (
        <span aria-hidden="true" className="tabular-nums">
          {value}
        </span>
      )}
    </span>
  );
}

interface SummaryFooterProps {
  /** `dataUpdatedAt` of the status query: when the last GOOD `/status` landed. */
  updatedAt: number;
  /** The status query is failing, so anything shown is last-known, not live. */
  error: boolean;
}

function SummaryFooter({ updatedAt, error }: SummaryFooterProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  // An EXTERNAL clock — the one kind of state an effect is still the right
  // home for. Deriving the age at render would freeze it between polls, which
  // is precisely the failure this surface exists to report.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), CLOCK_TICK_MS);
    return () => clearInterval(timer);
  }, []);

  // Nothing has landed yet: there is no age to state and no claim to qualify.
  if (updatedAt === 0) return null;

  const ageMs = Math.max(0, now - updatedAt);
  if (error || ageMs >= STALE_AFTER_MS) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-status-warning whitespace-nowrap">
        <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" aria-label={t('common.warning')} />
        {t('fleetMetrics.summary.stale', { age: formatDuration(ageMs / 1000, locale) })}
      </p>
    );
  }

  return (
    <p className={`text-xs whitespace-nowrap ${SECONDARY_TEXT_CLASS}`}>
      {t('fleetMetrics.summary.updated', { time: formatClock(updatedAt, locale) })}
    </p>
  );
}

export interface FleetSummaryCardProps {
  /** Live status. Renders Now and Down now; never waits on history. */
  status: FleetStatus | undefined;
  /** `dataUpdatedAt` of the status query. `0` until the first response lands. */
  statusUpdatedAt: number;
  /** The status query is in error; its data, if any, is stale. */
  statusError: boolean;
  /** One window's history. `undefined` while it loads and after it fails. */
  overview: FleetOverview | undefined;
  /** The preset whose period label heads the second column. */
  preset: TimeframeState['preset'];
  /** Nothing observed yet: rows with no figure are dropped rather than dashed. */
  firstRun: boolean;
}

export function FleetSummaryCard({
  status,
  statusUpdatedAt,
  statusError,
  overview,
  preset,
  firstRun,
}: FleetSummaryCardProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const headingId = useId();
  const periodLabel = t(TIMEFRAME_PERIOD_LABEL_KEY[preset]);

  const fromServer = overview !== undefined;
  const allRows: SummaryRow[] =
    overview?.summary.rows ??
    LIVE_ROW_ORDER.map((key) => ({ key, figure: null, previous: null, series: [] }));
  const peakRow = allRows.find((row) => row.key === PEAK_ROW);
  const rows = allRows.filter(
    // Peak is a sub-line of Down, and on a first run a row the server has no
    // figure for is not a row with a dash — there is nothing to say about it
    // yet. The pre-history floor above is exempt: its figures are null because
    // history has not answered, not because there is none.
    (row) => row.key !== PEAK_ROW && (!firstRun || !fromServer || row.figure !== null),
  );
  // The rule between the state block and the rate block. Derived from the row
  // keys, not a second hand-kept list of which rows are which.
  const firstRateIndex = rows.findIndex((row) => !hasNowColumn(row.key));

  const downNow = downNowPrinters(status);
  const overflow = Math.max(0, downNow.length - MAX_DOWN_ROWS);

  return (
    // `@container`, not a viewport breakpoint: this page's width changes with
    // the persisted sidebar toggle, so the card has to size against ITSELF.
    <section aria-labelledby={headingId} className="@container">
      <Card>
        <div className="px-6 py-4 border-b border-bambu-dark-tertiary flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 id={headingId} className="text-lg font-semibold text-white">
            {t('fleetMetrics.sections.fleet')}
          </h2>
          {overview !== undefined && (
            <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>
              {t('fleetMetrics.header.range', {
                from: formatSiteDate(overview.date_from, locale),
                to: formatSiteDate(overview.date_to, locale),
                bucket: t(`fleetMetrics.header.bucket.${overview.bucket}`),
                tz: overview.tz_name,
              })}
            </p>
          )}
        </div>

        <CardContent>
          {/* Fixed min-height: the period columns fill in after the Now column,
              and the rows below must not jump when they do. */}
          <div className="min-h-[18rem]">
            <table className="w-full text-sm">
              <caption className="sr-only">{t('fleetMetrics.sections.fleet')}</caption>
              <thead>
                <tr className={`text-xs ${SECONDARY_TEXT_CLASS}`}>
                  {/* The row-header column's corner cell names nothing. */}
                  <th scope="col" className="pb-2 text-left font-normal" />
                  <th scope="col" className="pb-2 px-3 text-right font-normal">
                    {t('fleetMetrics.summary.now')}
                  </th>
                  <th scope="col" className="pb-2 px-3 text-right font-normal">
                    {periodLabel}
                  </th>
                  <th
                    scope="col"
                    className="pb-2 px-3 text-left font-normal hidden @md:table-cell"
                  >
                    {t('fleetMetrics.summary.trend')}
                  </th>
                  <th scope="col" className="pb-2 pl-3 text-right font-normal">
                    {t('fleetMetrics.summary.vsPrevious')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => {
                  const format = SUMMARY_ROW_FORMAT[row.key];
                  const hintKey = SUMMARY_ROW_HINT_KEY[row.key];
                  const now = nowValue(row.key, status);
                  const change = readChange(row.figure, row.previous, {
                    precision: CHANGE_PRECISION[format],
                  });
                  const startsRateBlock = index === firstRateIndex && index > 0;
                  return (
                    <tr
                      key={row.key}
                      className={
                        startsRateBlock ? 'border-t border-bambu-dark-tertiary' : undefined
                      }
                    >
                      <th
                        scope="row"
                        className={`py-1.5 pr-3 text-left font-normal text-white ${startsRateBlock ? 'pt-3' : ''}`}
                      >
                        <span className="inline-flex items-center gap-1">
                          {t(SUMMARY_ROW_LABEL_KEY[row.key])}
                          {hintKey !== undefined && <InfoHint text={t(hintKey)} />}
                        </span>
                      </th>
                      <td
                        className={`py-1.5 px-3 text-right tabular-nums text-white ${startsRateBlock ? 'pt-3' : ''}`}
                      >
                        {now === null ? '' : formatCount(now, locale)}
                      </td>
                      <td
                        className={`py-1.5 px-3 text-right tabular-nums text-white ${startsRateBlock ? 'pt-3' : ''}`}
                      >
                        {row.figure === null ? (
                          <span className={SECONDARY_TEXT_CLASS}>—</span>
                        ) : (
                          formatFigure(row.figure, format, locale)
                        )}
                        {row.key === PEAK_HOST_ROW && peakRow !== undefined && peakRow.figure !== null && (
                          <span className={`block text-xs font-normal ${SECONDARY_TEXT_CLASS}`}>
                            {t('fleetMetrics.summary.peak', {
                              count: Math.round(peakRow.figure),
                            })}
                          </span>
                        )}
                      </td>
                      <td
                        className={`py-1.5 px-3 hidden @md:table-cell ${SECONDARY_TEXT_CLASS} ${startsRateBlock ? 'pt-3' : ''}`}
                      >
                        <Sparkline points={row.series} ariaLabel={t('fleetMetrics.summary.trend')} />
                      </td>
                      <td className={`py-1.5 pl-3 text-right ${startsRateBlock ? 'pt-3' : ''}`}>
                        <ChangeCell change={change} format={format} periodLabel={periodLabel} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="mt-3 pt-3 border-t border-bambu-dark-tertiary flex items-start justify-between gap-4">
            <div className="min-w-0 flex-1">
              <p className={`text-xs ${SECONDARY_TEXT_CLASS}`}>
                {t('fleetMetrics.summary.downNow')}
              </p>
              {/* Fixed min-height: the 30 s poll adds and removes rows here,
                  and the page below must not walk up and down with it. */}
              <ul className="mt-1 min-h-[5rem] space-y-1">
                {downNow.length === 0 && (
                  <li className={`text-sm ${SECONDARY_TEXT_CLASS}`}>
                    {t('fleetMetrics.summary.nothingDown')}
                  </li>
                )}
                {downNow.slice(0, MAX_DOWN_ROWS).map((printer) => {
                  const seconds =
                    status === undefined ? null : sinceSeconds(printer, status.generated_at);
                  return (
                    <li
                      key={printer.printer_id}
                      className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-sm"
                    >
                      <Link
                        to={`/?printer=${printer.printer_id}`}
                        className="text-white hover:text-bambu-green underline-offset-2 hover:underline rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50"
                      >
                        {printer.name}
                      </Link>
                      <span className={SECONDARY_TEXT_CLASS}>
                        {t(classLabelKey(printer.class_key))}
                      </span>
                      <span className={`tabular-nums ${SECONDARY_TEXT_CLASS}`}>
                        {seconds === null
                          ? '—'
                          : // `≥` when the walk back hit its bound: the class is at
                            // least this old, and stating it as exact would be a guess.
                            `${printer.since_open_ended ? '≥ ' : ''}${formatDuration(seconds, locale)}`}
                      </span>
                    </li>
                  );
                })}
                {overflow > 0 && (
                  <li className={`text-sm ${SECONDARY_TEXT_CLASS}`}>
                    {t('common.more', { count: overflow })}
                  </li>
                )}
              </ul>
            </div>
            <SummaryFooter updatedAt={statusUpdatedAt} error={statusError} />
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
