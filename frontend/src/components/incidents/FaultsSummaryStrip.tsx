/**
 * The Faults tab's headline figures over the window (kind + printer filters
 * applied, the outcome filter not — so filtering to one outcome never turns the
 * auto-recovered share into 0 % or 100 %).
 *
 * The auto-recovered share is `utils/incidents.clearedNoPersonShare`, the same
 * spelling the Fleet tab's Recovery headline reads.
 */
import { useTranslation } from 'react-i18next';
import { Card, CardContent } from '../Card';
import { NO_VALUE } from '../fleet/widgets/rows';
import { SECONDARY_TEXT_CLASS, formatCount, formatPercent } from '../../utils/fleetMetrics';
import { clearedNoPersonShare } from '../../utils/incidents';
import type { IncidentSummary } from '../../types/incidents';

interface FaultsSummaryStripProps {
  summary: IncidentSummary;
}

interface Figure {
  key: string;
  label: string;
  value: string;
  detail?: string;
}

export function FaultsSummaryStrip({ summary }: FaultsSummaryStripProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const share = clearedNoPersonShare(summary);
  const count = (value: number) => formatCount(value, locale);

  const figures: Figure[] = [
    { key: 'faults', label: t('incidents.summary.faults'), value: count(summary.total) },
    {
      key: 'autoRecovered',
      label: t('incidents.summary.autoRecovered'),
      value: count(summary.zero_human),
      detail: share === null ? NO_VALUE : formatPercent(share, locale),
    },
    {
      key: 'humanResolved',
      label: t('incidents.summary.humanResolved'),
      value: count(summary.by_outcome.human_resolved),
    },
    {
      key: 'resolvedUnpaged',
      label: t('incidents.summary.resolvedUnpaged'),
      value: count(summary.by_outcome.resolved_unpaged),
    },
    {
      key: 'openNow',
      label: t('incidents.summary.openNow'),
      value: count(summary.by_outcome.recovering + summary.by_outcome.held),
    },
    { key: 'maintenanceHolds', label: t('incidents.summary.maintenanceHolds'), value: count(summary.declared) },
  ];

  return (
    <Card>
      <CardContent>
        <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
          {figures.map((figure) => (
            <div key={figure.key} className="min-w-0">
              <dt className={`text-xs ${SECONDARY_TEXT_CLASS}`}>{figure.label}</dt>
              <dd className="mt-1 flex items-baseline gap-2">
                <span className="text-2xl font-semibold text-white tabular-nums">{figure.value}</span>
                {figure.detail !== undefined && (
                  <span className={`text-sm tabular-nums ${SECONDARY_TEXT_CLASS}`}>{figure.detail}</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}
