/**
 * FaultsTab — the Faults panel of the Stats page: every equipment fault and
 * maintenance hold opened in the picker's window, how it ended, and the
 * recurring signatures among them.
 *
 * The window is the SITE range the picker resolves to (`resolveSiteRange`,
 * unclamped — the ledger has no 366-day ceiling), so it waits on
 * `/fleet-metrics/status` for the site's today exactly as the Fleet tab does.
 * The summary and the recurring lines are window-wide and come from the server;
 * the table is one server page of `total` rows.
 *
 * The page index belongs to one window + filter set + page size: it is stored
 * WITH the scope it was chosen in, and any change of scope reads page 0 — a
 * derivation, not an effect resetting state after the fact.
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { api, INCIDENTS_MAX_LIMIT } from '../../api/client';
import { Button } from '../Button';
import { Card, CardContent } from '../Card';
import { InlineAlert } from '../ui/InlineAlert';
import { PAGE_SIZE_ALL } from '../ui/Pager';
import { FaultsFilters, type FaultsFilterValues, type FilterPrinter } from './FaultsFilters';
import { FaultsSummaryStrip } from './FaultsSummaryStrip';
import { FaultsTable } from './FaultsTable';
import { RecurringStrip } from './RecurringStrip';
import { resolveSiteRange, useFleetStatus } from '../../hooks/useFleetMetrics';
import { useIncidents } from '../../hooks/useIncidents';
import { SECONDARY_TEXT_CLASS } from '../../utils/fleetMetrics';
import type { IncidentsQuery } from '../../types/incidents';
import type { TimeframeState } from '../../utils/timeframe';

const DEFAULT_PAGE_SIZE = 50;

const NO_FILTERS: FaultsFilterValues = { kind: undefined, outcome: undefined, printerId: undefined };

export interface FaultsTabProps {
  /** The Stats header's picker. One selection serves every tab. */
  timeframe: TimeframeState;
}

export function FaultsTab({ timeframe }: FaultsTabProps) {
  const { t } = useTranslation();
  const statusQuery = useFleetStatus();
  const range = resolveSiteRange(timeframe, statusQuery.data);
  // The Stats page's printers query (same key, one cache entry).
  const { data: printerList } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });

  const [filters, setFilters] = useState<FaultsFilterValues>(NO_FILTERS);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const scope = [range?.dateFrom, range?.dateTo, filters.kind, filters.outcome, filters.printerId, pageSize].join('|');
  const [page, setPage] = useState({ scope, index: 0 });
  const pageIndex = page.scope === scope ? page.index : 0;

  const isAll = pageSize === PAGE_SIZE_ALL;
  const query: IncidentsQuery | undefined =
    range === undefined
      ? undefined
      : {
          dateFrom: range.dateFrom,
          dateTo: range.dateTo,
          kind: filters.kind,
          outcome: filters.outcome,
          printerId: filters.printerId,
          limit: isAll ? INCIDENTS_MAX_LIMIT : pageSize,
          offset: isAll ? 0 : pageIndex * pageSize,
        };
  const incidentsQuery = useIncidents(query);
  const response = incidentsQuery.data;

  const printers: FilterPrinter[] = (printerList ?? [])
    .map((printer) => ({ id: printer.id, name: printer.name }))
    .sort((left, right) => left.name.localeCompare(right.name));

  const statusFailed = statusQuery.isError && statusQuery.data === undefined;
  const failed = statusFailed || incidentsQuery.isError;
  // Status answered but the picker names no window (a custom range missing a date).
  const rangeUnresolved = statusQuery.data !== undefined && range === undefined;
  const loading = !failed && !rangeUnresolved && response === undefined;
  // `total`, not this page's rows: a page past a shrunken end still shows the Pager back.
  const empty = rangeUnresolved || (response !== undefined && response.total === 0);

  const retry = () => {
    void (statusFailed ? statusQuery.refetch() : incidentsQuery.refetch());
  };

  return (
    <div className="space-y-6">
      {response !== undefined && <FaultsSummaryStrip summary={response.summary} />}
      {response !== undefined && response.recurring.length > 0 && (
        <RecurringStrip
          lines={response.recurring}
          onSelectPrinter={(printerId) => setFilters({ ...filters, printerId })}
          onSelectKind={(kind) => setFilters({ ...filters, kind })}
        />
      )}
      <Card>
        <CardContent className="space-y-4">
          <FaultsFilters values={filters} printers={printers} onChange={setFilters} />
          {failed ? (
            <InlineAlert severity="error">
              <span className="flex flex-wrap items-center gap-3">
                <span>{t('incidents.states.loadFailed')}</span>
                <Button variant="secondary" size="sm" onClick={retry}>
                  {t('incidents.states.retry')}
                </Button>
              </span>
            </InlineAlert>
          ) : loading ? (
            <p role="status" className={`flex items-center gap-2 text-sm ${SECONDARY_TEXT_CLASS}`}>
              <Loader2 className="h-4 w-4 animate-spin text-bambu-green" aria-hidden="true" />
              {t('incidents.states.loading')}
            </p>
          ) : empty || response === undefined ? (
            <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>{t('incidents.states.empty')}</p>
          ) : (
            <FaultsTable
              response={response}
              dataUpdatedAt={incidentsQuery.dataUpdatedAt}
              pageIndex={pageIndex}
              pageSize={pageSize}
              onPageChange={(index) => setPage({ scope, index })}
              onPageSizeChange={setPageSize}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
