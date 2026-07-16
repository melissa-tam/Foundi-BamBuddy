/**
 * "Start ASAP / Schedule for later" control for a production run (Phase 5).
 *
 * Reuses the queue schedule field's date/time helpers (`utils/date`) and the
 * hidden `datetime-local` `.showPicker()` pattern. Emits a UTC ISO string (with
 * `Z`) for a future time, or `null` for ASAP; past/invalid entries are rejected
 * inline and reported via `onValidityChange` so the host dialog can block submit.
 *
 * One shared home for both the create dialog and the reschedule dialog.
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Calendar, CalendarClock, Clock } from 'lucide-react';
import {
  formatDateInput,
  formatTimeInput,
  getDatePlaceholder,
  getTimePlaceholder,
  parseDateInput,
  parseTimeInput,
  parseUTCDate,
  type DateFormat,
  type TimeFormat,
} from '../utils/date';

export interface ScheduledStartFieldProps {
  /** Current start time as a UTC ISO string (with `Z`), or `null` = start ASAP. */
  value: string | null;
  /** Emits a UTC ISO string for a future time, or `null` for ASAP. */
  onChange: (value: string | null) => void;
  /** Fires when the field's validity changes (false while scheduled + past/invalid). */
  onValidityChange?: (valid: boolean) => void;
  dateFormat?: DateFormat;
  timeFormat?: TimeFormat;
  /** Namespaces the input ids so two instances (create + reschedule) don't collide. */
  idPrefix?: string;
}

/** A sensible default deferred start: the next whole hour, one hour out. */
function defaultStart(): Date {
  const d = new Date();
  d.setHours(d.getHours() + 1, 0, 0, 0);
  return d;
}

export function ScheduledStartField({
  value,
  onChange,
  onValidityChange,
  dateFormat = 'system',
  timeFormat = 'system',
  idPrefix = 'run-schedule',
}: ScheduledStartFieldProps) {
  const { t } = useTranslation();
  const [scheduled, setScheduled] = useState(value != null);
  const [dateValue, setDateValue] = useState('');
  const [timeValue, setTimeValue] = useState('');
  const [isPast, setIsPast] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const hiddenInputRef = useRef<HTMLInputElement>(null);

  const dateId = `${idPrefix}-date`;
  const timeId = `${idPrefix}-time`;
  const errId = `${idPrefix}-error`;
  const hasError = scheduled && (invalid || isPast);

  // Seed the visible inputs from `value` (or a now+1h default) when scheduling.
  useEffect(() => {
    if (!scheduled) return;
    const seed = parseUTCDate(value) ?? defaultStart();
    setDateValue(formatDateInput(seed, dateFormat));
    setTimeValue(formatTimeInput(seed, timeFormat));
    setInvalid(false);
    setIsPast(false);
    // Re-seed only when the mode flips on, not on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scheduled]);

  const chooseAsap = () => {
    setScheduled(false);
    setInvalid(false);
    setIsPast(false);
    onChange(null);
    onValidityChange?.(true);
  };

  const chooseScheduled = () => {
    setScheduled(true);
    const seed = parseUTCDate(value) ?? defaultStart();
    onChange(seed.toISOString());
    onValidityChange?.(true);
  };

  const recompute = (nextDate: string, nextTime: string) => {
    const parsedDate = parseDateInput(nextDate, dateFormat);
    const parsedTime = parseTimeInput(nextTime);
    if (!parsedDate || !parsedTime) {
      setInvalid(true);
      setIsPast(false);
      onValidityChange?.(false);
      return;
    }
    setInvalid(false);
    parsedDate.setHours(parsedTime.hours, parsedTime.minutes, 0, 0);
    if (parsedDate.getTime() <= Date.now()) {
      setIsPast(true);
      onValidityChange?.(false);
      return;
    }
    setIsPast(false);
    onChange(parsedDate.toISOString());
    onValidityChange?.(true);
  };

  const handleDateChange = (v: string) => {
    setDateValue(v);
    recompute(v, timeValue);
  };
  const handleTimeChange = (v: string) => {
    setTimeValue(v);
    recompute(dateValue, v);
  };

  // Native datetime-local picker → local date; write it back into the fields.
  const handleCalendarChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    if (!v) return;
    const date = new Date(v);
    if (isNaN(date.getTime())) return;
    const nextDate = formatDateInput(date, dateFormat);
    const nextTime = formatTimeInput(date, timeFormat);
    setDateValue(nextDate);
    setTimeValue(nextTime);
    recompute(nextDate, nextTime);
  };

  const toggleBtn = (active: boolean) =>
    `flex-1 px-2 py-2 rounded-lg border text-sm flex items-center justify-center gap-1.5 transition-colors ${
      active
        ? 'bg-bambu-green border-bambu-green text-white'
        : 'bg-bambu-dark border-bambu-dark-tertiary text-bambu-gray hover:text-white'
    }`;

  return (
    <div className="space-y-3">
      <div>
        <span className="block text-sm text-bambu-gray mb-2">{t('productionRuns.schedule.whenToStart')}</span>
        <div className="flex gap-2" role="group" aria-label={t('productionRuns.schedule.whenToStart')}>
          <button type="button" className={toggleBtn(!scheduled)} aria-pressed={!scheduled} onClick={chooseAsap}>
            <Clock className="w-4 h-4" aria-hidden="true" />
            {t('productionRuns.schedule.asap')}
          </button>
          <button type="button" className={toggleBtn(scheduled)} aria-pressed={scheduled} onClick={chooseScheduled}>
            <CalendarClock className="w-4 h-4" aria-hidden="true" />
            {t('productionRuns.schedule.later')}
          </button>
        </div>
      </div>

      {scheduled && (
        <div>
          <div className="flex gap-2">
            <div className="flex-1 relative">
              <label htmlFor={dateId} className="block text-xs text-bambu-gray mb-1">
                {t('productionRuns.schedule.date')}
              </label>
              <input
                id={dateId}
                type="text"
                inputMode="numeric"
                className={`w-full px-3 py-2 pr-10 bg-bambu-dark border rounded-lg text-white focus:outline-none ${
                  invalid ? 'border-red-500' : 'border-bambu-dark-tertiary focus:border-bambu-green'
                }`}
                value={dateValue}
                onChange={(e) => handleDateChange(e.target.value)}
                placeholder={getDatePlaceholder(dateFormat)}
                aria-invalid={hasError}
                aria-describedby={hasError ? errId : undefined}
              />
              <button
                type="button"
                onClick={() => hiddenInputRef.current?.showPicker()}
                className="absolute right-2 top-[30px] text-bambu-gray hover:text-white"
                aria-label={t('productionRuns.schedule.openCalendar')}
              >
                <Calendar className="w-4 h-4" aria-hidden="true" />
              </button>
              {/* Hidden native picker anchored here so it opens near the date field. */}
              <input
                ref={hiddenInputRef}
                type="datetime-local"
                className="absolute bottom-0 left-0 w-0 h-0 opacity-0 pointer-events-none"
                onChange={handleCalendarChange}
                tabIndex={-1}
                aria-hidden="true"
              />
            </div>
            <div className="w-32">
              <label htmlFor={timeId} className="block text-xs text-bambu-gray mb-1">
                {t('productionRuns.schedule.time')}
              </label>
              <input
                id={timeId}
                type="text"
                className={`w-full px-3 py-2 bg-bambu-dark border rounded-lg text-white focus:outline-none ${
                  invalid || isPast ? 'border-red-500' : 'border-bambu-dark-tertiary focus:border-bambu-green'
                }`}
                value={timeValue}
                onChange={(e) => handleTimeChange(e.target.value)}
                placeholder={getTimePlaceholder(timeFormat)}
                aria-invalid={hasError}
                aria-describedby={hasError ? errId : undefined}
              />
            </div>
          </div>
          {hasError && (
            <p id={errId} role="alert" className="mt-1 text-xs text-red-400">
              {isPast ? t('productionRuns.schedule.pastError') : t('productionRuns.schedule.invalidError')}
            </p>
          )}
        </div>
      )}

      <p className="text-xs text-bambu-gray">
        {scheduled ? t('productionRuns.schedule.helpScheduled') : t('productionRuns.schedule.helpAsap')}
      </p>
    </div>
  );
}
