// One-line HMS summary shown on the printer card under the badge row. Surfaces
// the highest-severity active fault (its description, or the short code when the
// code is unknown) so the operator sees WHAT is wrong without opening the modal.
// Clicking it opens the full HMSErrorModal.
import { useTranslation } from 'react-i18next';
import { AlertTriangle, AlertCircle } from 'lucide-react';
import type { HMSError } from '../api/client';
import { hmsTone } from '../utils/hmsTone';

interface HMSErrorSummaryProps {
  errors: HMSError[];
  onOpen: () => void;
}

export function HMSErrorSummary({ errors, onOpen }: HMSErrorSummaryProps) {
  const { t } = useTranslation();
  if (!errors || errors.length === 0) return null;

  // Highest severity = lowest severity number (1=fatal … 4=info).
  const top = errors.reduce((worst, e) => (e.severity < worst.severity ? e : worst));
  const shortCode = top.short_code ?? '';
  const text = top.description ?? shortCode ?? '';

  // The summary can't see gcode_state under its prop contract, so its alert-role
  // keys off code severity; the card badge (which has state) owns the FAILED-state
  // escalation. error tone => a fatal/serious code is present.
  const tone = hmsTone(errors, undefined);
  const isError = tone === 'error';
  const Icon = isError ? AlertTriangle : AlertCircle;
  const colorClass = isError ? 'text-status-error' : 'text-status-warning';

  return (
    <button
      type="button"
      onClick={onOpen}
      title={text}
      className={`mt-1 flex w-full items-center gap-1.5 text-left text-xs ${colorClass} hover:underline`}
    >
      <Icon className="h-3.5 w-3.5 flex-shrink-0" />
      {/* alert lives on the text, not the button — role="alert" on the button
          would override its button role and hide the affordance from AT */}
      <span className="truncate" {...(isError ? { role: 'alert' } : {})}>
        {text || t('hmsErrors.unknownDescription')}
      </span>
    </button>
  );
}
