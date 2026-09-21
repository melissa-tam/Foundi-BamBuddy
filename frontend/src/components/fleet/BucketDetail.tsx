/**
 * The bucket detail — "why was 009 down 4 h on the 3rd?", answered in one
 * dialog: where the time went, what the printer made, and the timestamped
 * intervals and incidents it was all summed from.
 *
 * Opened from a matrix cell. The matrix already holds the aggregate (it drew
 * the cell), so the class split, the Time-split bar and the outcome counts
 * come from the cell it was handed — no second request for numbers already on
 * screen. The one thing the matrix does NOT have is the underlying intervals,
 * and this component owns that query, on demand, with its own loading, error
 * and empty states so a failed drill-down never blanks the grid behind it.
 *
 * ## Times are the SITE's, never the browser's
 *
 * Every instant on the wire is naive UTC. Each is shifted by the bucket's own
 * `utc_offset_minutes` and then read with UTC getters, so the dialog reads the
 * same in Kiritimati and in Los Angeles — the same rule `utils/fleetMetrics`
 * states for bucket labels. Nothing here calls a browser-zone formatter.
 *
 * ## What each bucket width asks for
 *
 * `/printers/{id}/intervals` takes inclusive SITE dates and 422s above seven
 * days, so: a DAY bucket asks for its own day; a WEEK bucket asks for its
 * seven; an HOUR bucket asks for the site day it falls in and shows only the
 * intervals that intersect the hour — the endpoint has no finer grain, and
 * asking for a day and filtering is cheaper than a narrower contract nobody
 * else needs.
 */
import { useId, useRef } from 'react';
import { Loader2, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { CardContent, CardHeader } from '../Card';
import { InlineAlert } from '../ui/InlineAlert';
import { Modal } from '../ui/Modal';
import { FleetSplitBar } from './FleetMatrixCell';
import { useFleetPrinterIntervals, type FleetRange } from '../../hooks/useFleetMetrics';
import type {
  ClassifiedInterval,
  FleetBucket,
  FleetGroup,
  MatrixCell,
  PrinterRef,
  SeriesBucket,
} from '../../types/fleetMetrics';
import {
  OUTCOME_ORDER,
  OUTCOME_LABEL_KEY,
  SECONDARY_TEXT_CLASS,
  bucketLabel,
  classLabelKey,
  foldTimeSplit,
  formatCount,
  formatDuration,
  formatHours,
  formatPercent,
  formatSiteInstant,
  parseClassKey,
  sumMap,
} from '../../utils/fleetMetrics';
import { addCalendarDays } from '../../utils/timeframe';

const SECONDS_PER_HOUR = 3600;

/** Class rows read top-down in the order an operator cares about them. */
const GROUP_RANK: Record<FleetGroup, number> = {
  printing: 0,
  cycle_overhead: 1,
  idle: 2,
  planned: 3,
  down: 4,
  unobserved: 5,
  not_recorded: 6,
  out_of_fleet: 7,
};

const EMPTY_CELL: MatrixCell = {
  class_seconds: {},
  down_seconds: 0,
  prints: {},
  basis: 'observed',
};

export interface BucketDetailProps {
  printer: PrinterRef;
  /** The matrix cell this dialog was opened from. Absent = nothing recorded. */
  cell: MatrixCell | undefined;
  /** The bucket itself — its start, width and site offset. */
  seriesBucket: SeriesBucket<unknown>;
  /** The bucket width the SERVER echoed. Decides what range is requested. */
  bucketWidth: FleetBucket;
  tzName: string;
  onClose: () => void;
}

export function BucketDetail({
  printer,
  cell,
  seriesBucket,
  bucketWidth,
  tzName,
  onClose,
}: BucketDetailProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const baseId = useId();
  const titleId = `${baseId}title`;
  const closeRef = useRef<HTMLButtonElement>(null);
  /**
   * Every section is a named region. Four unlabelled lists in one dialog are
   * four "list"s a screen-reader user has to read into to tell apart, and the
   * headings that name them are already on screen.
   */
  const headingId = (section: string): string => `${baseId}${section}`;

  const label = bucketLabel(seriesBucket, { bucket: bucketWidth, locale });
  const offset = seriesBucket.utc_offset_minutes;

  const range: FleetRange = {
    dateFrom: label.date,
    dateTo: bucketWidth === 'week' ? addCalendarDays(label.date, 6) : label.date,
  };
  const query = useFleetPrinterIntervals(printer.printer_id, range, { enabled: true });

  const bucketStartMs = Date.parse(`${seriesBucket.start}Z`);
  const bucketEndMs = bucketStartMs + seriesBucket.seconds * 1000;
  const intersectsBucket = (interval: ClassifiedInterval): boolean =>
    Date.parse(`${interval.end}Z`) > bucketStartMs && Date.parse(`${interval.start}Z`) < bucketEndMs;

  const response = query.data;
  const intervals =
    response === undefined
      ? []
      : bucketWidth === 'hour'
        ? response.intervals.filter(intersectsBucket)
        : response.intervals;
  const incidents = response?.incidents ?? [];

  const values = cell ?? EMPTY_CELL;
  const split = foldTimeSplit(values.class_seconds);
  const classTotal = sumMap(values.class_seconds);
  const classRows = Object.entries(values.class_seconds)
    .filter(([, seconds]) => (seconds ?? 0) > 0)
    .sort(([leftKey, leftSeconds], [rightKey, rightSeconds]) => {
      const rank = GROUP_RANK[parseClassKey(leftKey).group] - GROUP_RANK[parseClassKey(rightKey).group];
      return rank !== 0 ? rank : (rightSeconds ?? 0) - (leftSeconds ?? 0);
    });
  const outcomes = OUTCOME_ORDER.filter((outcome) => (values.prints[outcome] ?? 0) > 0);

  const at = (naiveUtc: string): string => formatSiteInstant(naiveUtc, offset, locale);
  /** An interval running up to the moment the answer was built is still open. */
  const ongoing = (end: string): boolean =>
    response !== undefined && Date.parse(`${end}Z`) >= Date.parse(`${response.generated_at}Z`);

  return (
    <Modal onClose={onClose} labelledBy={titleId} size="lg" initialFocusRef={closeRef}>
      {/*
        The close control is the dialog's FIRST focusable element and takes the
        initial focus. Without it the only focusable thing in here was "Open
        printer", at the bottom of a list that can run to nineteen hundred
        pixels — so opening the dialog scrolled the reader to its end, and a
        pointer user had no visible way out at all. Escape and the backdrop
        still close it, and `Modal` still returns focus to the cell that opened
        it.
      */}
      <CardHeader className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={titleId} className="text-base font-semibold text-white">
            {t('fleetMetrics.detail.title', {
              printer: printer.deleted
                ? t('fleetMetrics.matrix.deletedPrinter', { id: printer.printer_id })
                : printer.name,
              bucket: label.full,
            })}
          </h2>
          <p className={`text-xs ${SECONDARY_TEXT_CLASS}`}>{tzName}</p>
        </div>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label={t('common.close')}
          className={`shrink-0 rounded p-1 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50 ${SECONDARY_TEXT_CLASS}`}
        >
          <X className="h-5 w-5" aria-hidden="true" />
        </button>
      </CardHeader>

      <CardContent className="space-y-4">
        <section className="space-y-1" aria-labelledby={headingId('split')}>
          <h3 id={headingId('split')} className="text-sm font-medium text-white">
            {t('fleetMetrics.detail.timeSplit')}
          </h3>
          <FleetSplitBar split={split} heightClass="h-4" />
        </section>

        <section className="space-y-1" aria-labelledby={headingId('classes')}>
          <h3 id={headingId('classes')} className="text-sm font-medium text-white">
            {t('fleetMetrics.detail.classes')}
          </h3>
          {classRows.length === 0 ? (
            <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>{t('fleetMetrics.states.empty')}</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {classRows.map(([key, seconds]) => (
                  <tr key={key}>
                    <th scope="row" className="py-0.5 text-left font-normal">
                      {t(classLabelKey(key))}
                    </th>
                    <td className="py-0.5 text-right tabular-nums">
                      {formatHours((seconds ?? 0) / SECONDS_PER_HOUR, locale)}
                    </td>
                    <td className={`py-0.5 text-right tabular-nums ${SECONDARY_TEXT_CLASS}`}>
                      {classTotal > 0 ? formatPercent((seconds ?? 0) / classTotal, locale) : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <section className="space-y-1" aria-labelledby={headingId('prints')}>
          <h3 id={headingId('prints')} className="text-sm font-medium text-white">
            {t('fleetMetrics.detail.printsByOutcome')}
          </h3>
          {outcomes.length === 0 ? (
            <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>{t('fleetMetrics.detail.noPrints')}</p>
          ) : (
            <ul className="text-sm">
              {outcomes.map((outcome) => (
                <li key={outcome} className="flex justify-between py-0.5">
                  <span>{t(OUTCOME_LABEL_KEY[outcome])}</span>
                  <span className="tabular-nums">
                    {formatCount(values.prints[outcome] ?? 0, locale)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {query.isPending ? (
          <p role="status" className={`flex items-center gap-2 text-sm ${SECONDARY_TEXT_CLASS}`}>
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            {t('fleetMetrics.states.loading')}
          </p>
        ) : null}

        {query.isError ? (
          <InlineAlert severity="error">
            <span className="flex flex-wrap items-center gap-2">
              {t('fleetMetrics.states.loadFailed')}
              <button
                type="button"
                onClick={() => void query.refetch()}
                className="underline focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50"
              >
                {t('fleetMetrics.states.retry')}
              </button>
            </span>
          </InlineAlert>
        ) : null}

        {query.isSuccess ? (
          <>
            <section className="space-y-1" aria-labelledby={headingId('intervals')}>
              <h3 id={headingId('intervals')} className="text-sm font-medium text-white">
                {t('fleetMetrics.detail.intervals')}
              </h3>
              {intervals.length === 0 ? (
                <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>
                  {t('fleetMetrics.detail.noIntervals')}
                </p>
              ) : (
                <ul className="space-y-0.5 text-sm">
                  {intervals.map((interval) => (
                    <li key={`${interval.start}-${interval.class_key}`} className="flex justify-between gap-2">
                      <span className="tabular-nums">
                        {at(interval.start)} –{' '}
                        {ongoing(interval.end) ? t('fleetMetrics.detail.ongoing') : at(interval.end)}
                      </span>
                      <span className="text-right">
                        {t(classLabelKey(interval.class_key))}{' '}
                        <span className={SECONDARY_TEXT_CLASS}>
                          {formatDuration(interval.seconds, locale)}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="space-y-1" aria-labelledby={headingId('incidents')}>
              <h3 id={headingId('incidents')} className="text-sm font-medium text-white">
                {t('fleetMetrics.detail.incidents')}
              </h3>
              {incidents.length === 0 ? (
                <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>
                  {t('fleetMetrics.detail.noIncidents')}
                </p>
              ) : (
                <ul className="space-y-0.5 text-sm">
                  {incidents.map((incident) => (
                    <li key={incident.incident_id} className="flex justify-between gap-2">
                      <span className="tabular-nums">
                        {at(incident.created_at)} –{' '}
                        {incident.resolved_at === null
                          ? t('fleetMetrics.detail.ongoing')
                          : at(incident.resolved_at)}
                      </span>
                      <span>{t(`printers.incident.${incident.kind}`)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        ) : null}

        <Link
          to={`/?printer=${printer.printer_id}`}
          className="inline-block text-sm text-bambu-green hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50"
        >
          {t('fleetMetrics.detail.openPrinter')}
        </Link>
      </CardContent>
    </Modal>
  );
}
