import { useState, useCallback, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { MapPin, Plus, Loader2, Pencil, Trash2, X } from 'lucide-react';
import { api, type StorageLocation } from '../api/client';
import { Button } from './Button';
import { CardContent } from './Card';
import { Modal } from './ui/Modal';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { inventoryLocationsQueryKey, invalidateInventoryLocations } from '../utils/inventoryQueries';

interface LocationsModalProps {
  open: boolean;
  onClose: () => void;
  onPickLocation?: (locationId: number) => void;
}

export function LocationsModal({ open, onClose, onPickLocation }: LocationsModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<StorageLocation | null>(null);
  const [name, setName] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<StorageLocation | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);

  const { data: locations = [], isLoading } = useQuery({
    queryKey: inventoryLocationsQueryKey,
    queryFn: api.getLocations,
    enabled: open,
  });

  const invalidate = () => {
    invalidateInventoryLocations(queryClient);
    queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
    queryClient.invalidateQueries({ queryKey: ['spoolman-inventory-spools'] });
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      const trimmed = name.trim();
      if (!trimmed) throw new Error(t('locations.nameRequired'));
      if (editing) {
        return api.updateLocation(editing.id, { name: trimmed });
      }
      return api.createLocation({ name: trimmed });
    },
    onSuccess: () => {
      showToast(t(editing ? 'locations.updated' : 'locations.created'), 'success');
      setEditorOpen(false);
      setEditing(null);
      setName('');
      invalidate();
    },
    onError: (err: Error) => {
      showToast(err.message || t('locations.saveFailed'), 'error');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteLocation(id),
    onSuccess: () => {
      showToast(t('locations.deleted'), 'success');
      setDeleteTarget(null);
      invalidate();
    },
    onError: (err: Error) => {
      showToast(err.message || t('locations.deleteFailed'), 'error');
    },
  });

  const openCreate = () => {
    setEditing(null);
    setName('');
    setEditorOpen(true);
  };

  const openEdit = (location: StorageLocation) => {
    setEditing(location);
    setName(location.name);
    setEditorOpen(true);
  };

  const closeEditor = useCallback(() => {
    if (saveMutation.isPending) return;
    setEditorOpen(false);
    setEditing(null);
    setName('');
  }, [saveMutation.isPending]);

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    saveMutation.mutate();
  };

  if (!open) return null;

  const modalTitleId = 'locations-modal-title';
  const editorTitleId = 'location-editor-title';

  return (
    <>
      <Modal
        onClose={onClose}
        size="lg"
        labelledBy={modalTitleId}
        dismissDisabled={
          saveMutation.isPending || deleteMutation.isPending || editorOpen || deleteTarget !== null
        }
      >
        <CardContent className="p-0">
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-bambu-dark-tertiary">
          <div>
            <h2 id={modalTitleId} className="text-lg font-semibold text-white flex items-center gap-2">
              <MapPin className="w-5 h-5 text-bambu-green" />
              {t('locations.title')}
            </h2>
            <p className="text-bambu-gray text-sm mt-0.5">{t('locations.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button onClick={openCreate}>
              <Plus className="w-4 h-4" />
              {t('locations.add')}
            </Button>
            <button
              type="button"
              className="p-1.5 text-bambu-gray hover:text-white rounded"
              onClick={onClose}
              aria-label={t('common.close')}
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        <div className="overflow-y-auto">
          {isLoading ? (
            <div className="flex items-center justify-center py-16 text-bambu-gray">
              <Loader2 className="w-6 h-6 animate-spin mr-2" />
              {t('common.loading')}
            </div>
          ) : locations.length === 0 ? (
            <div className="py-16 text-center text-bambu-gray">{t('locations.empty')}</div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary text-left text-bambu-gray">
                  <th className="px-4 py-3 font-medium">{t('locations.name')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('locations.spools')}</th>
                  <th className="px-4 py-3 font-medium text-right w-32">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {locations.map((loc) => (
                  <tr
                    key={loc.id}
                    className="border-b border-bambu-dark-tertiary/60 hover:bg-bambu-dark-tertiary/30 cursor-pointer"
                    onClick={() => {
                      if (onPickLocation) {
                        onPickLocation(loc.id);
                        onClose();
                      }
                    }}
                  >
                    <td className="px-4 py-3 text-white font-medium">{loc.name}</td>
                    <td className="px-4 py-3 text-right text-bambu-gray">{loc.spool_count}</td>
                    <td className="px-4 py-3 text-right" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center justify-end gap-1">
                        <button
                          type="button"
                          className="p-1.5 text-bambu-gray hover:text-bambu-green rounded"
                          onClick={() => openEdit(loc)}
                          title={t('common.edit')}
                          aria-label={t('locations.editAria', { name: loc.name, defaultValue: `Edit ${loc.name}` })}
                        >
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          className="p-1.5 text-bambu-gray hover:text-red-400 rounded disabled:opacity-40"
                          disabled={loc.spool_count > 0}
                          onClick={() => setDeleteTarget(loc)}
                          title={loc.spool_count > 0 ? t('locations.deleteBlocked') : t('common.delete')}
                          aria-label={t('locations.deleteAria', { name: loc.name, defaultValue: `Delete ${loc.name}` })}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        </CardContent>
      </Modal>

      {editorOpen && (
        <Modal
          onClose={closeEditor}
          size="sm"
          overlayZIndex="z-[60]"
          labelledBy={editorTitleId}
          dismissDisabled={saveMutation.isPending}
          initialFocusRef={nameInputRef}
        >
          <CardContent className="p-6">
            <h3 id={editorTitleId} className="text-lg font-semibold text-white mb-4">
              {editing ? t('locations.edit') : t('locations.add')}
            </h3>
            <form onSubmit={handleSave}>
              <label className="block text-sm font-medium text-bambu-gray mb-1" htmlFor="location-name">
                {t('locations.name')}
              </label>
              <input
                id="location-name"
                ref={nameInputRef}
                type="text"
                maxLength={255}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green mb-4"
                placeholder={t('locations.createPlaceholder')}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <div className="flex justify-end gap-2">
                <Button type="button" variant="secondary" onClick={closeEditor}>
                  {t('common.cancel')}
                </Button>
                <Button type="submit" disabled={saveMutation.isPending || !name.trim()}>
                  {saveMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
                  {t('common.save')}
                </Button>
              </div>
            </form>
          </CardContent>
        </Modal>
      )}

      {deleteTarget && (
        <ConfirmModal
          title={t('locations.confirmDelete', { name: deleteTarget.name })}
          message={t('locations.confirmDeleteMessage')}
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={deleteMutation.isPending}
          onConfirm={() => deleteMutation.mutate(deleteTarget.id)}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </>
  );
}
