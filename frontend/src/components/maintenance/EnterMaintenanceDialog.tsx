/**
 * The enter-maintenance confirm: what the hold will stop (the farm's own live
 * actions, read off status by the host) and the optional return to service.
 * Presentational: the host owns the enter mutation and passes `pending` and
 * `onConfirm`. Shared by the printer card and the touch page.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ConfirmModal } from '../ConfirmModal';
import { ReturnToServiceField } from './ReturnToServiceField';
import type { DateFormat, TimeFormat } from '../../utils/date';

export interface EnterMaintenanceDialogProps {
  printerId: number;
  printerName: string;
  /** One line per live farm action the hold stops; the dialog body. */
  effects: string[];
  /** The enter request is in flight — blocks dismissal and submit. */
  pending: boolean;
  /** Enter, with the chosen return time (UTC ISO) or `null` = Manual. */
  onConfirm: (releaseAt: string | null) => void;
  onCancel: () => void;
  dateFormat?: DateFormat;
  timeFormat?: TimeFormat;
  /** Touch sizing (the touch view), passed through to the confirm. */
  touch?: boolean;
}

export function EnterMaintenanceDialog({
  printerId,
  printerName,
  effects,
  pending,
  onConfirm,
  onCancel,
  dateFormat,
  timeFormat,
  touch = false,
}: EnterMaintenanceDialogProps) {
  const { t } = useTranslation();
  const [releaseAt, setReleaseAt] = useState<string | null>(null);
  const [valid, setValid] = useState(true);

  return (
    <ConfirmModal
      title={t('printers.maintenance.confirmTitle', { name: printerName })}
      message={effects.join('\n')}
      confirmText={t('printers.maintenance.menuEnter')}
      variant="warning"
      isLoading={pending}
      confirmDisabled={!valid}
      onConfirm={() => onConfirm(releaseAt)}
      onCancel={onCancel}
      touch={touch}
    >
      <ReturnToServiceField
        value={releaseAt}
        onChange={setReleaseAt}
        onValidityChange={setValid}
        dateFormat={dateFormat}
        timeFormat={timeFormat}
        idPrefix={`maintenance-enter-${printerId}`}
      />
    </ConfirmModal>
  );
}
