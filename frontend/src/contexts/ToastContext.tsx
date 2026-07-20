import { AlertCircle, CheckCircle, Info, Loader2, X, XCircle } from 'lucide-react';
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';

export type ToastType = 'success' | 'error' | 'warning' | 'info' | 'loading';

/**
 * Auto-dismiss delay (ms) per severity. Errors linger longest so an operator
 * glancing away does not miss them; warnings sit in between; success/info keep
 * the original 3s. `loading` is auto-dismissed on the same 3s cadence as before
 * (long-running work should use showPersistentToast instead).
 *
 * Failure-surfacing convention (choose the SURFACE first, then the mechanism):
 *  - Dialog-internal mutation failure → inline alert INSIDE the dialog
 *    (`<InlineAlert>`), never a toast: the dialog is the surface in focus and a
 *    toast would vanish before the user reads it.
 *  - Page-level failure               → `showToast(message, 'error')`.
 *  - Long-running / actionable        → `showPersistentToast(id, message, ...)`.
 */
const TOAST_DURATION_MS: Record<ToastType, number> = {
  error: 8000,
  warning: 5000,
  success: 3000,
  info: 3000,
  loading: 3000,
};

export interface ToastAction {
  label: string;
  /** External-link actions carry an href (opened in a new tab). Pure in-app
   *  actions (the `actions[]` array path) supply only `onClick` and render as a
   *  <button>, so href is optional. */
  href?: string;
  onClick?: () => void;
}

type ShowPersistentToast = (
  id: string,
  message: string,
  type?: ToastType,
  /** `action` = a single legacy link-style action (unchanged, auto-dismisses on
   *  activation). `actions` = one or more in-app button actions that own their
   *  own dismissal (each `onClick` decides whether/when to clear the toast). */
  options?: { action?: ToastAction; actions?: ToastAction[] },
) => void;

interface Toast {
  id: string;
  message: string;
  type: ToastType;
  persistent?: boolean;
  action?: ToastAction;
  actions?: ToastAction[];
}

interface ToastContextType {
  showToast: (message: string, type?: ToastType) => void;
  showPersistentToast: ShowPersistentToast;
  dismissToast: (id: string) => void;
  /**
   * Suppress the visible toast viewport while keeping the state machine alive.
   * Used by the SpoolBuddy kiosk layout to keep the kiosk display free of
   * main-app notifications.
   */
  setViewportSuppressed: (suppressed: boolean) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error('useToast must be used within a ToastProvider');
  }
  return context;
}

const icons = {
  success: <CheckCircle className="w-5 h-5 text-green-400" />,
  error: <XCircle className="w-5 h-5 text-red-400" />,
  warning: <AlertCircle className="w-5 h-5 text-yellow-400" />,
  info: <Info className="w-5 h-5 text-blue-400" />,
  loading: <Loader2 className="w-5 h-5 text-bambu-green animate-spin" />,
};

const bgColors = {
  success: 'bg-green-500/10 border-green-500/30',
  error: 'bg-red-500/10 border-red-500/30',
  warning: 'bg-yellow-500/10 border-yellow-500/30',
  info: 'bg-blue-500/10 border-blue-500/30',
  loading: 'bg-bambu-green/10 border-bambu-green/30',
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [viewportSuppressed, setViewportSuppressed] = useState(false);
  const timeoutRefs = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // Tracks whether the provider is still mounted. A toast can be triggered by
  // an async callback that resolves AFTER React has unmounted us (common in
  // tests: `cleanup()` runs while a login promise is still in flight, then
  // the error handler calls showToast). In that case, scheduling a setTimeout
  // that later calls setToasts produces "window is not defined" once the jsdom
  // environment is torn down. Guard every setToasts call behind this ref so a
  // post-unmount showToast is a no-op instead of crashing.
  const isMountedRef = useRef(true);

  // Clean up all timeouts on unmount
  useEffect(() => {
    isMountedRef.current = true;
    const timeouts = timeoutRefs.current;
    return () => {
      isMountedRef.current = false;
      timeouts.forEach((timeout) => clearTimeout(timeout));
      timeouts.clear();
    };
  }, []);

  const showToast = useCallback((message: string, type: ToastType = 'success') => {
    if (!isMountedRef.current) return;
    const id = Math.random().toString(36).substr(2, 9);
    setToasts((prev) => [...prev, { id, message, type }]);

    // Auto-dismiss after a severity-dependent delay (errors linger longest).
    const timeout = setTimeout(() => {
      if (!isMountedRef.current) return;
      setToasts((prev) => prev.filter((t) => t.id !== id));
      timeoutRefs.current.delete(id);
    }, TOAST_DURATION_MS[type]);
    timeoutRefs.current.set(id, timeout);
  }, []);

  const showPersistentToast = useCallback(
    (
      id: string,
      message: string,
      type: ToastType = 'info',
      options?: { action?: ToastAction; actions?: ToastAction[] },
    ) => {
      if (!isMountedRef.current) return;
      setToasts((prev) => {
        // Update existing toast if same id, otherwise add new one
        const exists = prev.find((t) => t.id === id);
        if (exists) {
          return prev.map((t) =>
            t.id === id
              ? { ...t, message, type, persistent: true, action: options?.action, actions: options?.actions }
              : t,
          );
        }
        return [
          ...prev,
          { id, message, type, persistent: true, action: options?.action, actions: options?.actions },
        ];
      });
    },
    [],
  );

  const dismissToast = useCallback((id: string) => {
    if (!isMountedRef.current) return;
    // Clear any pending auto-dismiss timeout
    const timeout = timeoutRefs.current.get(id);
    if (timeout) {
      clearTimeout(timeout);
      timeoutRefs.current.delete(id);
    }
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ showToast, showPersistentToast, dismissToast, setViewportSuppressed }}>
      {children}

      {/* Toast Container — to the left of the bug-report bubble (bottom-4 right-4 w-12).
          The kiosk layout suppresses this entire viewport so SpoolBuddy displays stay
          free of main-app notifications. */}
      <div className={`fixed bottom-4 right-20 z-[60] flex flex-col items-end gap-2 ${viewportSuppressed ? 'hidden' : ''}`}>
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`rounded-lg border shadow-lg backdrop-blur-sm animate-slide-in ${bgColors[toast.type]} flex items-center gap-3 px-4 py-3`}
          >
            {icons[toast.type]}
            <span className="text-white text-sm">{toast.message}</span>
            {toast.actions && toast.actions.length > 0 &&
              // In-app button actions: each onClick owns its own dismissal
              // (e.g. dismiss only on success), so we do NOT auto-clear here.
              toast.actions.map((action, i) => (
                <button
                  key={`${toast.id}-action-${i}`}
                  type="button"
                  onClick={action.onClick}
                  className="ml-2 px-2 py-1 rounded text-xs font-medium bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 whitespace-nowrap"
                >
                  {action.label}
                </button>
              ))}
            {toast.action && (
              <a
                href={toast.action.href}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => {
                  // An action carrying its own onClick handles activation
                  // programmatically (e.g. in-app SPA navigation via
                  // react-router). Prevent the default new-tab open so we don't
                  // ALSO follow href. Actions with only an href (external links,
                  // e.g. the sponsor prompt) keep the default new-tab behavior.
                  if (toast.action?.onClick) {
                    e.preventDefault();
                    toast.action.onClick();
                  }
                  dismissToast(toast.id);
                }}
                className="ml-2 px-2 py-1 rounded text-xs font-medium bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 whitespace-nowrap"
              >
                {toast.action.label}
              </a>
            )}
            <button
              onClick={() => dismissToast(toast.id)}
              className="ml-2 text-bambu-gray hover:text-white transition-colors"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
