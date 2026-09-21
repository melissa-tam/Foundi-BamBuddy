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
 * ## First run
 *
 * Observation history starts the day the recorder ships and can never be
 * backfilled, so a fresh instance has a full live column and an empty period
 * column. That is stated once, as an info alert, and the summary drops the rows
 * it has nothing to say about — rather than printing a column of dashes that
 * reads like a fault.
 */
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { Button } from '../Button';
import { InfoHint } from '../ui/InfoHint';
import { InlineAlert } from '../ui/InlineAlert';
import { FleetSummaryCard } from './FleetSummaryCard';
import { resolveFleetRange, useFleetOverview, useFleetStatus } from '../../hooks/useFleetMetrics';
import { SECONDARY_TEXT_CLASS } from '../../utils/fleetMetrics';
import type { FleetOverview, FleetStatus } from '../../types/fleetMetrics';
import type { TimeframeState } from '../../utils/timeframe';

/**
 * A naive-UTC instant as a calendar day in the SITE's zone.
 *
 * The browser carries the IANA database and `/status` states the site's zone
 * name, so the day the recorder started can be named exactly without a second
 * offset table. An unrecognised zone name makes `Intl` throw, so the format
 * falls back to UTC rather than leaving a hole in the sentence.
 *
 * Distinct from the summary card's `formatSiteDate`, which converts a site
 * calendar DATE the server already resolved; this one converts an INSTANT.
 */
function formatInstantDay(instant: string, tzName: string, locale: string): string {
  const parsed = Date.parse(`${instant}Z`);
  if (Number.isNaN(parsed)) return instant;
  const options: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'short', day: 'numeric' };
  try {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone: tzName }).format(parsed);
  } catch {
    return new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' }).format(parsed);
  }
}

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

export function FleetTab({ timeframe }: FleetTabProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;

  const statusQuery = useFleetStatus();
  const status = statusQuery.data;
  // Pure, and `undefined` until `/status` has answered with the site's today.
  const range = resolveFleetRange(timeframe, status);
  const overviewQuery = useFleetOverview(range);
  const overview = overviewQuery.data;

  const firstRun = isFirstRun(status, overview);
  // Status answered but the picker resolves to no window at all — a custom
  // preset with a missing or reversed date. Nothing is loading; there is
  // simply nothing to ask for.
  const rangeUnresolved = status !== undefined && range === undefined;
  const historyLoading = !rangeUnresolved && overviewQuery.isPending && !overviewQuery.isError;
  const isEmpty =
    rangeUnresolved || (overview !== undefined && overview.fleet_series.buckets.length === 0);

  return (
    <div className="space-y-6">
      {firstRun && status !== undefined && (
        <InlineAlert severity="info">
          {t('fleetMetrics.states.firstRun', {
            date:
              status.recording_since === null
                ? // Recording begins with this build, so the site's today IS
                  // the day it started.
                  formatInstantDay(`${status.site_today}T00:00:00`, 'UTC', locale)
                : formatInstantDay(status.recording_since, status.tz_name, locale),
            earliest:
              status.history_since === null
                ? formatInstantDay(`${status.site_today}T00:00:00`, 'UTC', locale)
                : formatInstantDay(status.history_since, status.tz_name, locale),
          })}
        </InlineAlert>
      )}

      <FleetSummaryCard
        status={status}
        statusUpdatedAt={statusQuery.dataUpdatedAt}
        statusError={statusQuery.isError}
        overview={overview}
        preset={timeframe.preset}
        firstRun={firstRun}
      />

      {/* One section-level state row, always in the flow, so resolving a load
          never shifts the card above it or the sections below. */}
      <div className="min-h-[2.75rem]">
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
        ) : isEmpty ? (
          <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>{t('fleetMetrics.states.empty')}</p>
        ) : null}
      </div>

      {/*
        INTEGRATION SEAM — the fleet matrix and the widget grid mount here.
        In scope at this point: `overview` (FleetOverview | undefined),
        `status` (FleetStatus | undefined), `range` (FleetRange | undefined)
        and the `gridKey` prop above.
      */}
    </div>
  );
}
