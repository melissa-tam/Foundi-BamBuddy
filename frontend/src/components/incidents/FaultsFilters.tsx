/**
 * The Faults tab's three filters. Each `<select>` carries its one visible
 * `<label htmlFor>`; an empty value means "all".
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import type { PrinterIncidentKind } from '../../api/client';
import { FAULT_KIND_ORDER, incidentKindLabelKey } from '../../utils/fleetMetrics';
import { INCIDENT_OUTCOMES, outcomeLabelKey } from '../../utils/incidents';
import type { IncidentOutcome } from '../../types/incidents';

/** Every kind the ledger can hold: the faults in display order, then the declared hold. */
const FILTER_KINDS: readonly PrinterIncidentKind[] = [...FAULT_KIND_ORDER, 'service_hold'];

export interface FaultsFilterValues {
  kind: PrinterIncidentKind | undefined;
  outcome: IncidentOutcome | undefined;
  printerId: number | undefined;
}

export interface FilterPrinter {
  id: number;
  name: string;
}

interface FaultsFiltersProps {
  values: FaultsFilterValues;
  printers: readonly FilterPrinter[];
  onChange: (next: FaultsFilterValues) => void;
}

const SELECT_CLASS =
  'w-full rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary px-3 py-1.5 text-sm text-white focus:outline-none focus:border-bambu-green';
const LABEL_CLASS = 'mb-1 block text-xs text-bambu-gray';

const isKind = (value: string): value is PrinterIncidentKind =>
  (FILTER_KINDS as readonly string[]).includes(value);
const isOutcome = (value: string): value is IncidentOutcome =>
  (INCIDENT_OUTCOMES as readonly string[]).includes(value);

export function FaultsFilters({ values, printers, onChange }: FaultsFiltersProps) {
  const { t } = useTranslation();
  const kindId = useId();
  const outcomeId = useId();
  const printerId = useId();

  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <div>
        <label htmlFor={kindId} className={LABEL_CLASS}>
          {t('incidents.filters.kind')}
        </label>
        <select
          id={kindId}
          className={SELECT_CLASS}
          value={values.kind ?? ''}
          onChange={(event) => {
            const value = event.target.value;
            onChange({ ...values, kind: isKind(value) ? value : undefined });
          }}
        >
          <option value="">{t('incidents.filters.allKinds')}</option>
          {FILTER_KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {t(incidentKindLabelKey(kind))}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label htmlFor={outcomeId} className={LABEL_CLASS}>
          {t('incidents.filters.outcome')}
        </label>
        <select
          id={outcomeId}
          className={SELECT_CLASS}
          value={values.outcome ?? ''}
          onChange={(event) => {
            const value = event.target.value;
            onChange({ ...values, outcome: isOutcome(value) ? value : undefined });
          }}
        >
          <option value="">{t('incidents.filters.allOutcomes')}</option>
          {INCIDENT_OUTCOMES.map((outcome) => (
            <option key={outcome} value={outcome}>
              {t(outcomeLabelKey(outcome))}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label htmlFor={printerId} className={LABEL_CLASS}>
          {t('incidents.filters.printer')}
        </label>
        <select
          id={printerId}
          className={SELECT_CLASS}
          value={values.printerId === undefined ? '' : String(values.printerId)}
          onChange={(event) => {
            const value = Number(event.target.value);
            onChange({ ...values, printerId: event.target.value === '' || !Number.isFinite(value) ? undefined : value });
          }}
        >
          <option value="">{t('incidents.filters.allPrinters')}</option>
          {printers.map((printer) => (
            <option key={printer.id} value={printer.id}>
              {printer.name}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
