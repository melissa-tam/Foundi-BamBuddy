/**
 * FleetTab — the Fleet panel of the Stats page.
 *
 * ## Two queries, on purpose
 *
 * `/status` is live and polls; `/overview` sweeps a window and can take
 * seconds. Running them as one request would make "is anything down right now"
 * wait on a year of history, so they are two queries and the summary card is
 * built to render with either one missing. `resolveFleetRange` sits between
 * them because the Fleet tab's "today" is the SITE's, which only `/status`
 * knows — that is why the history query is disabled until status answers, and
 * why nothing here reads the browser's clock to decide a date.
 *
 * ## States live at SECTION level
 *
 * Loading, error and empty are reported once, under the card, in a row with a
 * fixed min-height — so resolving a load never moves the card or anything
 * below it. Errors keep Now alive: a failed history sweep says nothing about
 * whether a printer is down, and blanking the live column because the slow
 * query failed would hide the one fact worth acting on.
 *
 * No skeletons anywhere: `animate-pulse` already means "live" in this app, and
 * a pulsing placeholder would read as a printer doing something.
 *
 * ## The recording notice
 *
 * Observation history starts the day the recorder ships and can never be
 * backfilled, while the print log and the fault ledger go back weeks. So on a
 * real instance nearly every window an operator picks is MOSTLY unrecorded —
 * that is the normal first month of this tab's life, not an edge case, and the
 * tab has to read honestly in it.
 *
 * The notice therefore fires on the WINDOW (`windowPrecedesRecording`), not on
 * "has anything ever been observed": the old condition was true only on a
 * virgin instance, so the one state it was written for — a 44-day window over
 * an hour-old recorder — said nothing at all.
 *
 * `firstRun` is a different and much narrower question, and keeps its own name:
 * it decides whether the summary DROPS the rows it has nothing to say about,
 * rather than printing a column of dashes that reads like a fault.
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { Button } from '../Button';
import { Card, CardContent, CardHeader } from '../Card';
import { InfoHint } from '../ui/InfoHint';
import { InlineAlert } from '../ui/InlineAlert';
import { FleetMatrix } from './FleetMatrix';
import { FleetSummaryCard } from './FleetSummaryCard';
import { FleetWidgets } from './FleetWidgets';
import { resolveFleetRange, useFleetOverview, useFleetStatus } from '../../hooks/useFleetMetrics';
import {
  SECONDARY_TEXT_CLASS,
  formatInstantSiteDay,
  formatSiteDate,
  windowPrecedesRecording,
} from '../../utils/fleetMetrics';
import type { FleetOverview, FleetStatus } from '../../types/fleetMetrics';
import type { TimeframeState } from '../../utils/timeframe';

/**
 * The vertical space the matrix and the widget grid stand in for while history
 * is loading or after it failed.
 *
 * A reserve, not a guess at the final height: the point is that the state row
 * above it and the page furniture around it do not sit on a collapsed page and
 * then get shoved when a 30-day sweep lands. It is deliberately about one
 * viewport of content — enough that the transition reads as filling in rather
 * than as the page jumping.
 */
const SECTION_RESERVE_CLASS = 'min-h-[32rem]';

/**
 * The recording notice's slot, ALWAYS in the flow.
 *
 * The notice's own condition is not known until `/status` lands, and an alert
 * that appears a moment after first paint pushes the summary card — the hero
 * this tab's whole geometry was already pinned to hold still (its row floor,
 * its reserved peak sub-line, its fixed list min-heights all exist for exactly
 * that reason). So the slot is reserved rather than conditional, and only its
 * CONTENT arrives late.
 *
 * `2.875rem` is one `InlineAlert` measured: `p-3` top and bottom (24 px), one
 * `text-sm` line at 20 px, and the 1 px border either side. The notice wraps to
 * a second line below roughly 720 px of card width, where the slot grows with
 * it — the reserve is what keeps the steady desktop case still, not a cap.
 */
const NOTICE_SLOT_CLASS = 'min-h-[2.875rem]';

/**
 * Nothing has been observed yet.
 *
 * Read from the DATA rather than from a flag: `recording_since` is null before
 * the recorder's first write, and a window whose every bucket rests on the
 * incident ledger alone is the same situation seen from the history side.
 */
function isFirstRun(status: FleetStatus | undefined, overview: FleetOverview | undefined): boolean {
  if (status === undefined) return false;
  if (status.recording_since === null) return true;
  if (overview === undefined) return false;
  const buckets = overview.fleet_series.buckets;
  return buckets.length > 0 && buckets.every((bucket) => bucket.basis === 'incidents_only');
}

/**
 * The timeframe picker's basis hint for the Fleet tab.
 *
 * Its own component, mounted only while the Fleet tab is open, because the zone
 * it names comes from `/status` — and a hint rendered on the Prints tab would
 * start a 30 s classifier poll nobody is reading. It shares the status query
 * key with `FleetTab`, so the two cost one request.
 */
export function FleetTimeframeHint() {
  const { t } = useTranslation();
  const { data: status } = useFleetStatus();
  // No zone yet, no claim about day boundaries. The hint appears when it can
  // state something true.
  if (status === undefined) return null;
  return <InfoHint text={t('fleetMetrics.hints.timeframeFleet', { tz: status.tz_name })} />;
}

export interface FleetTabProps {
  /** The Stats header's picker. One selection serves both tabs. */
  timeframe: TimeframeState;
  /**
   * Remount key for the widget grid mounted below at the integration step.
   * Bumped by the header's "Reset layout" so the grid re-reads its stored
   * layout — the same mechanism the Prints grid uses.
   */
  gridKey?: number;
}

export function FleetTab({ timeframe, gridKey }: FleetTabProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const matrixHeadingId = useId();

  const statusQuery = useFleetStatus();
  const status = statusQuery.data;
  // Pure, and `undefined` until `/status` has answered with the site's today.
  const range = resolveFleetRange(timeframe, status);
  const overviewQuery = useFleetOverview(range);
  const overview = overviewQuery.data;

  const firstRun = isFirstRun(status, overview);
  /**
   * Does this window reach back before the recorder did? Read from `/status`
   * and the RESOLVED window, so it is answerable the moment status lands and
   * never waits on the history sweep. An unresolved window (a custom preset
   * with a missing or reversed date) has nothing to say about.
   */
  const recordingGap =
    status !== undefined &&
    range !== undefined &&
    windowPrecedesRecording(range.dateFrom, status.recording_since, status.tz_name);
  // Status answered but the picker resolves to no window at all — a custom
  // preset with a missing or reversed date. Nothing is loading; there is
  // simply nothing to ask for.
  const rangeUnresolved = status !== undefined && range === undefined;
  const historyLoading = !rangeUnresolved && overviewQuery.isPending && !overviewQuery.isError;
  const isEmpty =
    rangeUnresolved || (overview !== undefined && overview.fleet_series.buckets.length === 0);

  return (
    <div className="space-y-6">
      <div className={NOTICE_SLOT_CLASS}>
        {recordingGap && status !== undefined && (
          <InlineAlert severity="info">
            {t('fleetMetrics.states.recordingGap', {
              date:
                status.recording_since === null
                  ? // Recording begins with this build, so the site's today IS
                    // the day it started — and `site_today` is already a site
                    // calendar DATE, not an instant to be converted.
                    formatSiteDate(status.site_today, locale)
                  : formatInstantSiteDay(status.recording_since, status.tz_name, locale),
              earliest:
                status.history_since === null
                  ? formatSiteDate(status.site_today, locale)
                  : formatInstantSiteDay(status.history_since, status.tz_name, locale),
            })}
          </InlineAlert>
        )}
      </div>

      <FleetSummaryCard
        status={status}
        statusUpdatedAt={statusQuery.dataUpdatedAt}
        statusError={statusQuery.isError}
        overview={overview}
        preset={timeframe.preset}
        range={range}
        firstRun={firstRun}
        recordingGap={recordingGap}
      />

      {/*
        The matrix SECTION is always in the flow, and history's loading, error
        and empty states live INSIDE it. They used to sit in a reserved row of
        their own between the two cards, which cost ~83 px of dead space in the
        steady state — a permanent gap to report a condition that is normally
        absent. Inside the section they occupy space the section had reserved
        anyway, and the matrix card's top edge never moves.

        A refetch keeps the previous window on screen
        (`placeholderData: keepPreviousData`), so changing the timeframe redraws
        the old numbers rather than blanking the page.
      */}
      <section aria-labelledby={matrixHeadingId} className={SECTION_RESERVE_CLASS}>
        <Card>
          <CardHeader>
            <h2 id={matrixHeadingId} className="text-lg font-semibold text-white">
              {t('fleetMetrics.sections.matrix')}
            </h2>
          </CardHeader>
          <CardContent>
            {overviewQuery.isError ? (
              <InlineAlert severity="error">
                <span className="flex flex-wrap items-center gap-3">
                  <span>{t('fleetMetrics.states.loadFailed')}</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      void overviewQuery.refetch();
                    }}
                  >
                    {t('fleetMetrics.states.retry')}
                  </Button>
                </span>
              </InlineAlert>
            ) : historyLoading ? (
              <p role="status" className={`flex items-center gap-2 text-sm ${SECONDARY_TEXT_CLASS}`}>
                <Loader2 className="w-4 h-4 animate-spin text-bambu-green" aria-hidden="true" />
                {t('fleetMetrics.states.loading')}
              </p>
            ) : isEmpty || overview === undefined ? (
              <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>{t('fleetMetrics.states.empty')}</p>
            ) : (
              <FleetMatrix overview={overview} status={status} />
            )}
          </CardContent>
        </Card>
      </section>

      {overview !== undefined && !isEmpty && (
        <>
          {/*
            `gridKey` is the header's Reset-layout remount key: clearing the
            stored layout does not itself re-render the grid, which reads
            localStorage once at mount, so the page bumps the key and the grid
            comes back on its defaults. Same mechanism the Prints grid uses.
          */}
          <FleetWidgets key={gridKey} overview={overview} />
        </>
      )}
    </div>
  );
}
