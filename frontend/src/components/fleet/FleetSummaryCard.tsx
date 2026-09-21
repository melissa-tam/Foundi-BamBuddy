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
import { Card, CardContent, CardHeader } from '../Card';
import { InfoHint } from '../ui/InfoHint';
import { Sparkline } from './Sparkline';
import { TIMEFRAME_PERIOD_LABEL_KEY, type FleetRange } from '../../hooks/useFleetMetrics';
import {
  DOWN_CAUSE_ORDER,
  SECONDARY_TEXT_CLASS,
  SUMMARY_ROW_FORMAT,
  SUMMARY_ROW_LABEL_KEY,
  SUMMARY_ROW_NOW_GROUP,
  SUMMARY_ROW_ORDER,
  SUMMARY_ROW_UNIT_KEY,
  classLabelKey,
  formatCount,
  formatDuration,
  formatPercent,
  formatPoints,
  formatPrinters,
  formatSiteDate,
  headerRangeKey,
  isSingleDayRange,
  readChange,
  summaryHintKeys,
  type ChangeReading,
} from '../../utils/fleetMetrics';
import type {
  FleetOverview,
  FleetStatus,
  PrinterStatus,
  SummaryRow,
  SummaryRowKey,
} from '../../types/fleetMetrics';
import type { TimeframeState } from '../../utils/timeframe';

/** The formats `SUMMARY_ROW_FORMAT` assigns a row. */
type RowFormat = (typeof SUMMARY_ROW_FORMAT)[SummaryRowKey];

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
  return key === 'printers_in_fleet' || SUMMARY_ROW_NOW_GROUP[key] !== undefined;
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
  const group = SUMMARY_ROW_NOW_GROUP[key];
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
 * `HH:MM` on the READER's clock — the footer states when their page last
 * updated.
 *
 * `hourCycle: 'h23'`, never `hour12: false`: the latter selects the locale's
 * OWN 24-hour cycle, which ICU makes **h24** for several of them, so a page
 * that last refreshed at midnight reported "Updated 24:00". Same fix and same
 * reason as the hour buckets in `utils/fleetMetrics.bucketLabel`.
 */
function formatClock(epochMs: number, locale: string): string {
  return new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
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
    // change", so it is never rendered as a zero, and the accessible name says
    // WHICH claim it is. A bare "No data" here was ambiguous with the row's own
    // figure being missing, which is a different fact entirely.
    return (
      <span
        role="img"
        aria-label={t('fleetMetrics.change.noPrevious', { period: periodLabel })}
        className={SECONDARY_TEXT_CLASS}
      >
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
  /**
   * The window the client resolved, so the header can name the dates before
   * the server has echoed anything. Undefined until `/status` answers.
   */
  range: FleetRange | undefined;
  /** Nothing observed yet: rows with no figure are dropped rather than dashed. */
  firstRun: boolean;
  /**
   * The window reaches back before the recorder did, so every period figure in
   * this card covers only the recorded part of it. Qualifies the tooltips the
   * hinted rows already carry; the tab states the fact itself, once, above.
   */
  recordingGap: boolean;
}

export function FleetSummaryCard({
  status,
  statusUpdatedAt,
  statusError,
  overview,
  preset,
  range,
  firstRun,
  recordingGap,
}: FleetSummaryCardProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const headingId = useId();
  const periodLabel = t(TIMEFRAME_PERIOD_LABEL_KEY[preset]);

  /**
   * The header's one line. The loaded form names the bucket as well; the
   * pending form states everything that is already known and nothing that is
   * not. Null only before `/status` has answered, when there is no window to
   * name at all.
   */
  // A ONE-DAY window names its date once. "Sep 21, 2026 – Sep 21, 2026" is a
  // range whose two ends a reader has to compare character by character to
  // discover it is a single day; which leaf says so is the util's rule.
  const headerLine: string | null =
    overview !== undefined
      ? t(headerRangeKey(isSingleDayRange(overview.date_from, overview.date_to), false), {
          from: formatSiteDate(overview.date_from, locale),
          to: formatSiteDate(overview.date_to, locale),
          date: formatSiteDate(overview.date_from, locale),
          bucket: t(`fleetMetrics.header.bucket.${overview.bucket}`),
          tz: overview.tz_name,
        })
      : range !== undefined && status !== undefined
        ? t(headerRangeKey(isSingleDayRange(range.dateFrom, range.dateTo), true), {
            from: formatSiteDate(range.dateFrom, locale),
            to: formatSiteDate(range.dateTo, locale),
            date: formatSiteDate(range.dateFrom, locale),
            tz: status.tz_name,
          })
        : null;

  const fromServer = overview !== undefined;
  // The floor is the WHOLE row set, not just the rows `/status` can fill.
  // Every row is rendered from first paint in its final geometry — the state
  // rows carrying their live Now value, the rate rows carrying "—" until
  // history lands — so the card's bounding box does not change when it does.
  // A floor of only the live rows made the card grow 89 px on arrival and
  // shoved the matrix and the entire widget grid down the page.
  const allRows: SummaryRow[] =
    overview?.summary.rows ??
    SUMMARY_ROW_ORDER.map((key) => ({ key, figure: null, previous: null, series: [] }));
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
  const [downNowExpanded, setDownNowExpanded] = useState(false);

  return (
    // `@container`, not a viewport breakpoint: this page's width changes with
    // the persisted sidebar toggle, so the card has to size against ITSELF.
    <section aria-labelledby={headingId} className="@container">
      <Card>
        <CardHeader className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 id={headingId} className="text-lg font-semibold text-white">
            {t('fleetMetrics.sections.fleet')}
          </h2>
          {/*
            Stated from FIRST PAINT: the resolved window and the site's zone are
            both known as soon as `/status` answers, and the reader should not
            have to wait on a 90-day sweep to learn which days they are looking
            at. The bucket joins when the server echoes its own choice.

            One line, always — `truncate` rather than wrapping, with the full
            sentence on the element. A header that gained a second line when the
            bucket arrived would move the card, which is the very thing this
            card is being fixed for; and a truncated value's full text belongs
            in a tooltip (react-best-practices §9).
          */}
          {headerLine !== null && (
            <p
              className={`min-w-0 flex-1 truncate text-sm ${SECONDARY_TEXT_CLASS}`}
              title={headerLine}
            >
              {headerLine}
            </p>
          )}
        </CardHeader>

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
                  const hintKeys = summaryHintKeys(row.key, { recordingGap });
                  const unitKey = SUMMARY_ROW_UNIT_KEY[row.key];
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
                          {/* One hint per row, whatever it has to say: the
                              window qualifier joins the row's own sentence
                              rather than arriving as a second trigger beside
                              it (react-best-practices §9). */}
                          {hintKeys.length > 0 && (
                            <InfoHint text={hintKeys.map((key) => t(key)).join(' ')} />
                          )}
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
                          <>
                            {formatFigure(row.figure, format, locale)}
                            {/* The unit, where the figure is not a figure
                                without one: a bare "Prints 21" beside a matrix
                                total of 900 reads as a count of prints rather
                                than as a daily rate. `SUMMARY_ROW_FORMAT` says
                                how a number is spelled and deliberately not
                                what it counts per — that is its own map. */}
                            {unitKey !== undefined && (
                              <span className={`ml-1 text-xs font-normal ${SECONDARY_TEXT_CLASS}`}>
                                {t(unitKey)}
                              </span>
                            )}
                          </>
                        )}
                        {/* The sub-line is RESERVED, not conditional: it is
                            the Down row's second line whether or not a peak is
                            known yet, so the row — and the card, and everything
                            below it — is the same height before and after the
                            history sweep answers. */}
                        {row.key === PEAK_HOST_ROW && (
                          <span className={`block text-xs font-normal ${SECONDARY_TEXT_CLASS}`}>
                            {peakRow?.figure == null
                              ? ' '
                              : t('fleetMetrics.summary.peak', {
                                  count: Math.round(peakRow.figure),
                                })}
                          </span>
                        )}
                      </td>
                      <td
                        className={`py-1.5 px-3 hidden @md:table-cell ${SECONDARY_TEXT_CLASS} ${startsRateBlock ? 'pt-3' : ''}`}
                      >
                        {/* Named for ITS OWN row. Ten sparklines all called
                            "Trend" are ten identical images to a screen-reader
                            user, and the column header cannot disambiguate them
                            because it is the same header for all ten. */}
                        <Sparkline
                          points={row.series}
                          ariaLabel={t('fleetMetrics.summary.trendOf', {
                            metric: t(SUMMARY_ROW_LABEL_KEY[row.key]),
                          })}
                        />
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
                {downNow.slice(0, downNowExpanded ? downNow.length : MAX_DOWN_ROWS).map((printer) => {
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
                {(overflow > 0 || downNowExpanded) && (
                  <li>
                    {/*
                      A real control, not a caption. "+9 more" that cannot be
                      pressed tells the operator there are nine more printers
                      down and then refuses to name them — on a lights-out farm
                      that is the most important list on the page.

                      The growth is USER-initiated, so it is allowed to move the
                      page; the 30 s poll still never does, because the
                      collapsed list keeps its fixed min-height.
                    */}
                    <button
                      type="button"
                      aria-expanded={downNowExpanded}
                      onClick={() => setDownNowExpanded((shown) => !shown)}
                      className={`rounded text-sm underline-offset-2 hover:text-white hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50 ${SECONDARY_TEXT_CLASS}`}
                    >
                      {downNowExpanded
                        ? t('fleetMetrics.summary.showFewer')
                        : t('common.more', { count: overflow })}
                    </button>
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
