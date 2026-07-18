import { AlertCircle, AlertTriangle, Info, type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

/**
 * InlineAlert — a persistent, in-flow status box for surfaces the user is
 * looking at when something fails (e.g. a mutation error inside an open
 * dialog, where a toast would vanish before it is read).
 *
 * Extracted from the duplicated red alert markup that lived across the
 * production-run dialogs. Colors use paired light/dark shades so the box reads
 * correctly in both themes (class-based `dark:` variant) rather than the
 * dark-only `text-red-300` the originals assumed.
 */

export type InlineAlertSeverity = 'error' | 'warning' | 'info';

interface InlineAlertProps {
  severity: InlineAlertSeverity;
  children: ReactNode;
  /** Extra utility classes appended to the container (e.g. spacing). */
  className?: string;
}

const SEVERITY_STYLES: Record<
  InlineAlertSeverity,
  { container: string; icon: string; Icon: LucideIcon }
> = {
  error: {
    container: 'bg-red-500/10 border-red-500/40 text-red-700 dark:text-red-300',
    icon: 'text-red-600 dark:text-red-400',
    Icon: AlertCircle,
  },
  warning: {
    container: 'bg-yellow-500/10 border-yellow-500/40 text-yellow-700 dark:text-yellow-300',
    icon: 'text-yellow-600 dark:text-yellow-400',
    Icon: AlertTriangle,
  },
  info: {
    container: 'bg-blue-500/10 border-blue-500/40 text-blue-700 dark:text-blue-300',
    icon: 'text-blue-600 dark:text-blue-400',
    Icon: Info,
  },
};

export function InlineAlert({ severity, children, className = '' }: InlineAlertProps) {
  const { container, icon, Icon } = SEVERITY_STYLES[severity];

  return (
    <div
      role="alert"
      className={`flex items-start gap-2 p-3 border rounded-lg text-sm ${container} ${className}`.trim()}
    >
      <Icon className={`w-4 h-4 flex-shrink-0 mt-0.5 ${icon}`} />
      <span>{children}</span>
    </div>
  );
}
