/**
 * WaitingReason — one inline "why is this held" line: an alert icon plus the
 * humanized/translated waiting-reason copy. Renders nothing when the reason maps
 * to no copy. When the reason was a bare machine token the raw token is kept in
 * `title` so it stays inspectable on hover without becoming headline text; a
 * backend-authored sentence carries no title (it is already the copy).
 *
 * The single presentation for a farm waiting reason — the queue rows use it so
 * they never render a raw token again.
 */
import { AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { waitingReasonText, isTokenShaped } from '../../utils/waitingReason';

export function WaitingReason({ reason, className }: { reason: string; className?: string }) {
  const { t } = useTranslation();
  const text = waitingReasonText(reason, t);
  if (!text) return null;
  return (
    <p
      className={`flex items-start gap-1 text-purple-600 dark:text-purple-400 ${className ?? ''}`}
      title={isTokenShaped(reason) ? reason : undefined}
    >
      <AlertCircle className="w-3 h-3 mt-0.5 flex-shrink-0" aria-hidden="true" />
      <span>{text}</span>
    </p>
  );
}

export default WaitingReason;
