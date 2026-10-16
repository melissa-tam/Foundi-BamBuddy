/**
 * The maintenance hold's optional "Return to service" time: the shared
 * `ScheduledStartField` with the hold's wording ("Manual" = no scheduled
 * return, the hold ends only on Exit). One home for that wording, used by the
 * enter dialog and the banner's Change dialog, on the card and the touch page.
 */
import { useTranslation } from 'react-i18next';
import { ScheduledStartField } from '../ScheduledStartField';
import type { DateFormat, TimeFormat } from '../../utils/date';

export interface ReturnToServiceFieldProps {
  /** Current return time as a UTC ISO string, or `null` = Manual. */
  value: string | null;
  /** Emits a future UTC ISO string, or `null` for Manual. */
  onChange: (value: string | null) => void;
  /** False while a scheduled time is past or malformed — block submit on it. */
  onValidityChange: (valid: boolean) => void;
  dateFormat?: DateFormat;
  timeFormat?: TimeFormat;
  /** Namespaces the input ids (several cards render on one page). */
  idPrefix: string;
}

export function ReturnToServiceField({
  value,
  onChange,
  onValidityChange,
  dateFormat,
  timeFormat,
  idPrefix,
}: ReturnToServiceFieldProps) {
  const { t } = useTranslation();
  return (
    <ScheduledStartField
      value={value}
      onChange={onChange}
      onValidityChange={onValidityChange}
      dateFormat={dateFormat}
      timeFormat={timeFormat}
      idPrefix={idPrefix}
      labels={{
        heading: t('printers.maintenance.returnHeading'),
        asap: t('printers.maintenance.returnManual'),
        later: t('printers.maintenance.returnAt'),
      }}
    />
  );
}
