/**
 * The recurring fault signatures of the window — derived server-side
 * (`printer_incidents.recurring_signatures`), rendered only when there is at
 * least one line (the caller decides; no empty state, no "all clear").
 *
 * Each line's printer and fault are buttons that narrow the tab's table to that
 * printer / fault type. The rule that decides what is listed is stated ONCE, in
 * the heading's tooltip, never inline.
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, CardContent, CardHeader } from '../Card';
import { InfoHint } from '../ui/InfoHint';
import { formatHours, formatSiteInstant, SECONDARY_TEXT_CLASS, SITE_DATE_FORMAT } from '../../utils/fleetMetrics';
import type { PrinterIncidentKind } from '../../api/client';
import type { RecurringLine } from '../../types/incidents';

interface RecurringStripProps {
  lines: RecurringLine[];
  onSelectPrinter: (printerId: number) => void;
  onSelectKind: (kind: PrinterIncidentKind) => void;
}

const LINK_CLASS =
  'rounded text-white underline decoration-dotted underline-offset-2 hover:text-bambu-green focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50';

const SEPARATOR = ' · ';

export function RecurringStrip({ lines, onSelectPrinter, onSelectKind }: RecurringStripProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;
  const headingId = useId();

  const faultButton = (line: RecurringLine) => {
    // `description (code)` when the catalog has text, the code alone otherwise — never the code twice.
    const description = line.printer_message?.description ?? '';
    return (
      <button type="button" className={LINK_CLASS} onClick={() => onSelectKind(line.kind)}>
        {description === '' ? line.code : `${description} (${line.code})`}
      </button>
    );
  };

  const printerButton = (printerId: number, name: string | null) => (
    <button type="button" className={LINK_CLASS} onClick={() => onSelectPrinter(printerId)}>
      {name ?? `#${printerId}`}
    </button>
  );

  const facts = (line: RecurringLine): string[] => {
    const common = [
      t('incidents.recurring.holdsOnDays', { holds: line.holds, days: line.days }),
      t('incidents.recurring.held', {
        duration: `${formatHours(line.held_s / 3600, locale)} ${t('fleetMetrics.units.hours')}`,
      }),
    ];
    const last = t('incidents.recurring.last', { when: formatSiteInstant(line.last_at, line.utc_offset_minutes, locale, SITE_DATE_FORMAT) });
    return line.scope === 'fleet'
      ? [t('incidents.recurring.printersAffected', { n: line.printers_affected, total: line.roster_size }), ...common, last]
      : [...common, t('incidents.recurring.fleetMedian', { n: line.fleet_median }), last];
  };

  return (
    <section aria-labelledby={headingId}>
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <h2 id={headingId} className="text-lg font-semibold text-white">
              {t('incidents.recurring.heading')}
            </h2>
            <InfoHint text={t('incidents.recurring.hint')} />
          </div>
        </CardHeader>
        <CardContent>
          <ul role="list" className="space-y-2 text-sm">
            {lines.map((line) => (
              <li key={`${line.scope}:${line.printer_id ?? 'fleet'}:${line.kind}:${line.code}`}>
                {line.scope === 'fleet' || line.printer_id === null ? (
                  <span className="font-medium text-white">{t('incidents.recurring.fleetScope')}</span>
                ) : (
                  printerButton(line.printer_id, line.printer_name)
                )}
                {SEPARATOR}
                {faultButton(line)}
                <span className={SECONDARY_TEXT_CLASS}>
                  {SEPARATOR}
                  {facts(line).join(SEPARATOR)}
                </span>
                {line.scope === 'fleet' && line.worst.length > 0 && (
                  <span className={SECONDARY_TEXT_CLASS}>
                    {SEPARATOR}
                    {t('incidents.recurring.worst')}{' '}
                    {line.worst.map((worst, index) => (
                      <span key={worst.printer_id}>
                        {index > 0 && ', '}
                        {printerButton(worst.printer_id, worst.printer_name)} {worst.holds}
                      </span>
                    ))}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </section>
  );
}
