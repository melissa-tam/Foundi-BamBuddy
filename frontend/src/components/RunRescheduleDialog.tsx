/**
 * Reschedule dialog — change (or clear) a scheduled run's start time (Phase 5).
 *
 * Shared by the production-run list (`ProductionRunsPage`) and the run detail
 * page (`ProductionRunDetailPage`) so there is one canonical implementation.
 * Presentational + props-driven: the owning page holds the reschedule mutation
 * and passes `saving`/`error`/`onSubmit`. A future ISO string reschedules the
 * run; `null` (a cleared or past time) starts it now. Modal size, copy and
 * past-time validation are unchanged from the original in-page dialog.
 */
import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { CalendarClock, Loader2, X } from 'lucide-react';
import { api } from '../api/client';
import { CardContent } from './Card';
import { Button } from './Button';
import { Modal } from './ui/Modal';
import { InlineAlert } from './ui/InlineAlert';
import { ScheduledStartField } from './ScheduledStartField';
import type { ProductionRun } from '../types/productionRuns';

export interface RunRescheduleDialogProps {
  run: ProductionRun;
  /** Reschedule/start-now request in flight — blocks dismissal + submit. */
  saving: boolean;
  /** Backend failure detail from the last attempt, rendered inline. */
  error: string | null;
  /** Future ISO string reschedules; `null` starts the run now. */
  onSubmit: (at: string | null) => void;
  onClose: () => void;
}

export function RunRescheduleDialog({ run, saving, error, onSubmit, onClose }: RunRescheduleDialogProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const [value, setValue] = useState<string | null>(run.scheduled_start_at);
  const [valid, setValid] = useState(true);

  return (
    <Modal onClose={onClose} size="sm" dismissDisabled={saving} labelledBy={titleId}>
      <CardContent className="p-0">
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <CalendarClock className="w-5 h-5 text-bambu-green" />
            <h2 id={titleId} className="text-lg font-semibold text-white">{t('productionRuns.rescheduleTitle')}</h2>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={saving} aria-label={t('common.close')}>
            <X className="w-5 h-5" />
          </Button>
        </div>
        <div className="p-4 space-y-4">
          <p className="text-sm text-bambu-gray">{run.name}</p>
          <ScheduledStartField
            value={value}
            onChange={setValue}
            onValidityChange={setValid}
            dateFormat={settings?.date_format || 'system'}
            timeFormat={settings?.time_format || 'system'}
            idPrefix="run-reschedule-schedule"
          />
          {error && <InlineAlert severity="error">{error}</InlineAlert>}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={onClose} disabled={saving}>
              {t('common.cancel')}
            </Button>
            <Button onClick={() => onSubmit(value)} disabled={saving || !valid}>
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              {t('common.save')}
            </Button>
          </div>
        </div>
      </CardContent>
    </Modal>
  );
}

export default RunRescheduleDialog;
