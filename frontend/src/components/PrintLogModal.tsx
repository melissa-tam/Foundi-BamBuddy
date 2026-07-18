import { useTranslation } from 'react-i18next';
import { X, History } from 'lucide-react';
import { PrintLogTable } from './PrintLogTable';
import { Modal } from './ui/Modal';

interface PrintLogModalProps {
  archiveId: number;
  archiveName: string | null;
  onClose: () => void;
}

export function PrintLogModal({ archiveId, archiveName, onClose }: PrintLogModalProps) {
  const { t } = useTranslation();

  return (
    <Modal onClose={onClose} labelledBy="print-log-modal-title" size="lg" className="flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 min-w-0">
            <History className="w-5 h-5 text-bambu-green flex-shrink-0" />
            <h2 id="print-log-modal-title" className="text-lg font-semibold text-white truncate" title={archiveName || ''}>
              {t('archives.runLog.modalTitle', { name: archiveName || t('archives.runLog.modalTitleFallback') })}
            </h2>
          </div>
          <button
            onClick={onClose}
            className="text-bambu-gray hover:text-white transition-colors"
            title={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-6 overflow-y-auto flex-1">
          <PrintLogTable archiveId={archiveId} />
        </div>
    </Modal>
  );
}
