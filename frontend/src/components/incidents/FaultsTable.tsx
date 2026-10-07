/**
 * One page of the fault ledger, newest first, with the shared `Pager`.
 *
 * The Held column of an OPEN row is live: a 1 s tick runs only while the page
 * shows an open row, and `utils/incidents.liveHeldSeconds` advances the
 * server's figure by the time since the response landed. Closed rows never
 * re-render on a clock.
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { INCIDENTS_MAX_LIMIT } from '../../api/client';
import { PAGE_SIZE_ALL, Pager } from '../ui/Pager';
import {
  SECONDARY_TEXT_CLASS,
  formatDuration,
  formatSiteInstant,
  incidentKindLabelKey,
} from '../../utils/fleetMetrics';
import {
  isOpenOutcome,
  liveHeldSeconds,
  outcomeBadgeClass,
  outcomeLabelKey,
  resolveSourceLabelKey,
} from '../../utils/incidents';
import type { IncidentRow, IncidentsResponse } from '../../types/incidents';

const TICK_MS = 1000;

interface FaultsTableProps {
  response: IncidentsResponse;
  /** `dataUpdatedAt` of the query that produced `response`. */
  dataUpdatedAt: number;
  pageIndex: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

/** A clock that ticks once a second while `running`, and holds still otherwise. */
function useTick(running: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(timer);
  }, [running]);
  return now;
}

/** A job id the printer actually minted (`""` / `"0"` mean none). */
const hasJobId = (jobId: string): boolean => jobId !== '' && jobId !== '0';

const HEAD_CLASS = `px-3 py-2 text-left text-xs font-medium ${SECONDARY_TEXT_CLASS}`;
const CELL_CLASS = 'px-3 py-2 align-top';

export function FaultsTable({
  response,
  dataUpdatedAt,
  pageIndex,
  pageSize,
  onPageChange,
  onPageSizeChange,
}: FaultsTableProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const now = useTick(response.items.some((row) => isOpenOutcome(row.outcome)));
  const isAll = pageSize === PAGE_SIZE_ALL;
  const totalPages = isAll ? 1 : Math.max(1, Math.ceil(response.total / pageSize));

  const closedBy = (row: IncidentRow): string => {
    const label = resolveSourceLabelKey(row.resolve_source);
    return 'key' in label ? t(label.key) : label.raw;
  };

  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label={t('fleetMetrics.tabs.faults')}>
          <thead>
            <tr className="border-b border-bambu-dark-tertiary">
              <th scope="col" className={HEAD_CLASS}>{t('incidents.columns.opened')}</th>
              <th scope="col" className={HEAD_CLASS}>{t('incidents.columns.printer')}</th>
              <th scope="col" className={HEAD_CLASS}>{t('incidents.columns.fault')}</th>
              <th scope="col" className={HEAD_CLASS}>{t('incidents.columns.unit')}</th>
              <th scope="col" className={HEAD_CLASS}>{t('incidents.columns.outcome')}</th>
              <th scope="col" className={HEAD_CLASS}>{t('incidents.columns.closedBy')}</th>
              <th scope="col" className={`${HEAD_CLASS} text-right`}>{t('incidents.columns.held')}</th>
            </tr>
          </thead>
          <tbody>
            {response.items.map((row) => {
              const description = row.printer_messages[0]?.description ?? '';
              return (
                <tr key={row.id} className="border-b border-bambu-dark-tertiary/50">
                  <td className={`${CELL_CLASS} whitespace-nowrap text-white`}>
                    {formatSiteInstant(row.created_at, row.utc_offset_minutes, locale)}
                  </td>
                  <td className={`${CELL_CLASS} whitespace-nowrap text-white`}>
                    {row.printer_name ?? `#${row.printer_id}`}
                  </td>
                  <td className={CELL_CLASS}>
                    <div className="flex flex-wrap items-center gap-2 text-white">
                      <span>{t(incidentKindLabelKey(row.kind))}</span>
                      {row.recurring && (
                        <span className="rounded-full border border-status-warning/20 bg-status-warning/10 px-2 py-0.5 text-xs text-status-warning">
                          {t('incidents.recurring.marker')}
                        </span>
                      )}
                    </div>
                    {/* The description line only when the catalog has text: the code is in the meta line below. */}
                    {description !== '' && <div className="text-white">{description}</div>}
                    <div className={`text-xs ${SECONDARY_TEXT_CLASS}`}>
                      {[row.code, row.slot_desc, row.external ? t('incidents.external') : null]
                        .filter((part): part is string => part !== null && part !== '')
                        .join(' · ')}
                    </div>
                  </td>
                  <td className={`${CELL_CLASS} whitespace-nowrap`}>
                    <div className="text-white">
                      {row.item_id === null ? t('incidents.foreign') : t('incidents.unit', { id: row.item_id })}
                    </div>
                    {hasJobId(row.job_id) && (
                      <div className={`text-xs ${SECONDARY_TEXT_CLASS}`}>{t('incidents.job', { id: row.job_id })}</div>
                    )}
                  </td>
                  <td className={CELL_CLASS}>
                    <span
                      className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-medium ${outcomeBadgeClass(row.outcome)}`}
                    >
                      {t(outcomeLabelKey(row.outcome))}
                    </span>
                  </td>
                  <td className={`${CELL_CLASS} text-white`}>{closedBy(row)}</td>
                  <td className={`${CELL_CLASS} whitespace-nowrap text-right tabular-nums text-white`}>
                    {formatDuration(liveHeldSeconds(row, dataUpdatedAt, now), locale)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {isAll && response.total > INCIDENTS_MAX_LIMIT && (
        <p className={`text-sm ${SECONDARY_TEXT_CLASS}`}>
          {t('incidents.states.truncated', { limit: INCIDENTS_MAX_LIMIT, total: response.total })}
        </p>
      )}
      <Pager
        pageIndex={pageIndex}
        pageSize={pageSize}
        totalRows={response.total}
        totalPages={totalPages}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
        unitLabel={t('incidents.units.faults')}
        t={t}
      />
    </div>
  );
}
