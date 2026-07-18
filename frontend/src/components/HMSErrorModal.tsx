// HMS Error Modal — renders the printer's active HMS faults.
//
// The human-readable descriptions, canonical short code, and wiki link now ride
// the API payload (backend services/hms_errors.py — ONE table). This component
// no longer holds a code→description table and no longer hides codes it doesn't
// recognise: an unknown/novel fault renders with an explicit fallback message so
// a lights-out farm never shows a faulting printer as "OK" (H1).
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { X, AlertTriangle, AlertCircle, Info, ExternalLink, Loader2, Trash2 } from 'lucide-react';
import type { HMSError, Permission } from '../api/client';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { formatHmsCode } from '../utils/hmsCode';
import { Modal } from './ui/Modal';
import { CardContent } from './Card';

interface HMSErrorModalProps {
  printerName: string;
  errors: HMSError[];
  onClose: () => void;
  printerId: number;
  hasPermission: (permission: Permission) => boolean;
}

type SeverityKey = 'fatal' | 'serious' | 'warning' | 'info';

function getSeverityInfo(severity: number): {
  severityKey: SeverityKey;
  color: string;
  bgColor: string;
  buttonHoverColor: string;
  Icon: typeof AlertTriangle;
} {
  switch (severity) {
    case 1:
      return { severityKey: 'fatal', color: 'text-red-500', bgColor: 'bg-red-500/20', buttonHoverColor: 'bg-red-500/10', Icon: AlertTriangle };
    case 2:
      return { severityKey: 'serious', color: 'text-red-400', bgColor: 'bg-red-500/15', buttonHoverColor: 'bg-red-500/10', Icon: AlertTriangle };
    case 3:
      return { severityKey: 'warning', color: 'text-orange-400', bgColor: 'bg-orange-500/20', buttonHoverColor: 'bg-orange-500/10', Icon: AlertCircle };
    case 4:
    default:
      return { severityKey: 'info', color: 'text-blue-400', bgColor: 'bg-blue-500/20', buttonHoverColor: 'bg-blue-500/10', Icon: Info };
  }
}

export function HMSErrorModal({ printerName, errors, onClose, printerId, hasPermission }: HMSErrorModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const clearMutation = useMutation({
    mutationFn: () => api.clearHMSErrors(printerId),
    onSuccess: () => {
      showToast(t('hmsErrors.clearSuccess'), 'success');
      onClose();
    },
    onError: () => {
      showToast(t('hmsErrors.clearFailed'), 'error');
    },
  });

  // printerStatusMutation with optimistic update
  const activateActionMutation = useMutation({
    mutationFn: (data: {
      action: string,
      print_error: string,
      job_id: string | null,
    }) => api.executeHMSAction(printerId, {
      action: data.action,
      print_error: data.print_error,
      job_id: data.job_id,
    }),
    onSuccess: () => {
      // Scope the invalidation to THIS printer. The prefix form
      // `['printerStatus']` would refresh every printer card on the page,
      // which is wasteful when only one printer's state actually changed.
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
      showToast(t('hmsErrors.actionSuccess', 'Action sent to printer'), 'success');
      onClose();
    },
    onError: (error: Error) => {
      showToast(
        `${t('hmsErrors.actionFailed', 'Failed to send action')}: ${error.message}`,
        'error',
      );
    },
  });

  return (
    <Modal onClose={onClose} closeOnOverlay={false} labelledBy="hms-error-modal-title">
      <CardContent className="p-0">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <AlertTriangle className="w-5 h-5 text-orange-400" />
            <h2 id="hms-error-modal-title" className="text-lg font-semibold text-white">{t('hmsErrors.title', { name: printerName })}</h2>
          </div>
          <button
            onClick={onClose}
            className="p-1 hover:bg-bambu-dark-tertiary rounded-lg transition-colors"
          >
            <X className="w-5 h-5 text-bambu-gray" />
          </button>
        </div>

        {/* Content */}
        <div className="p-4">
          {errors.length === 0 ? (
            <div className="text-center py-8 text-bambu-gray">
              <AlertCircle className="w-12 h-12 mx-auto mb-3 opacity-30" />
              <p>{t('hmsErrors.noErrors')}</p>
            </div>
          ) : (
            <div className="space-y-3">
              {errors.map((error, index) => {
                const { severityKey, color, bgColor, buttonHoverColor, Icon } = getSeverityInfo(error.severity);
                const shortCode = error.short_code ?? '';
                // Backend descriptions are vendor fault strings (kept English, like
                // the printer's own screen); the fallback below IS translated.
                const description = error.description ?? t('hmsErrors.unknownDescription');
                // Show the FULL firmware code (16-hex hms[]-array faults render as
                // four hyphen-groups) instead of the lossy two-group short code.
                const displayCode = formatHmsCode(error.full_code, error.short_code);

                return (
                  <div
                    key={`${error.code}-${index}`}
                    className={`p-4 rounded-lg ${bgColor} border border-white/10`}
                  >
                    <div className="flex items-start gap-3">
                      <Icon className={`w-5 h-5 ${color} flex-shrink-0 mt-0.5`} />
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-1">
                          {displayCode && <span className={`font-mono text-sm ${color}`}>[{displayCode}]</span>}
                          <span className={`text-xs px-2 py-0.5 rounded-full ${bgColor} ${color}`}>
                            {t(`hmsErrors.severity.${severityKey}`)}
                          </span>
                        </div>
                        <p className="text-sm text-bambu-gray mb-2">{description}</p>
                        {error.actions && error.actions.length > 0 && (
                          <div className="flex flex-wrap gap-2 my-2">
                            {error.actions.map((action) => (
                              <button
                                key={action}
                                onClick={() => {
                                  // full_code is the firmware-matching key (16
                                  // chars for hms[]-array faults, 8 chars for
                                  // print_error). Fall back to the short code for
                                  // older backends that haven't populated it. See #1830.
                                  activateActionMutation.mutate({
                                    action,
                                    print_error: error.full_code || shortCode.replace('_', ''),
                                    job_id: error.job_id ?? null,
                                  });
                                }}
                                className={`flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg ${bgColor} ${color} hover:${buttonHoverColor} transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0`}
                              >
                                {t(`hmsErrors.actions.${action}`, action)}
                              </button>
                            ))}
                          </div>
                        )}
                        {error.wiki_url && (
                          <a
                            href={error.wiki_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-xs text-bambu-green hover:underline"
                          >
                            <ExternalLink className="w-3 h-3" />
                            {t('hmsErrors.viewOnWiki')}
                          </a>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-4 border-t border-bambu-dark-tertiary flex items-center justify-between gap-3">
          <p className="text-xs text-bambu-gray">
            {t('hmsErrors.clearInstructions')}
          </p>
          {errors.length > 0 && (
            <button
              onClick={() => clearMutation.mutate()}
              disabled={!hasPermission('printers:control') || clearMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
            >
              {clearMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )}
              {t('hmsErrors.clearErrors')}
            </button>
          )}
        </div>
      </CardContent>
    </Modal>
  );
}
