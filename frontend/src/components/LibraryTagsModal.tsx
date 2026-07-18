import { useState, useCallback, useRef } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Tag, Plus, Loader2, Pencil, Trash2, X } from 'lucide-react';

import { api, type LibraryTag } from '../api/client';
import { Button } from './Button';
import { CardContent } from './Card';
import { Modal } from './ui/Modal';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { libraryTagsQueryKey } from '../utils/libraryTagsQuery';

interface LibraryTagsModalProps {
  open: boolean;
  onClose: () => void;
  /** Optional callback when the user clicks a row to pick a tag for filtering. */
  onPickTag?: (tagId: number) => void;
}

/**
 * Catalog CRUD for #1268 library tags. Same shape as LocationsModal but tags
 * are deletable while in use — the backend's ON DELETE CASCADE drops the
 * association rows, files keep their identity. The confirm dialog warns the
 * user when file_count > 0 so accidental deletion of a heavily-used tag isn't
 * silent.
 */
export function LibraryTagsModal({ open, onClose, onPickTag }: LibraryTagsModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<LibraryTag | null>(null);
  const [name, setName] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<LibraryTag | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);

  const { data: tags = [], isLoading } = useQuery({
    queryKey: libraryTagsQueryKey,
    queryFn: api.getLibraryTags,
    enabled: open,
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: libraryTagsQueryKey });
    // File listings carry the tags array — bump those too so chips refresh
    // immediately after a rename/delete.
    queryClient.invalidateQueries({ queryKey: ['library-files'] });
  };

  const saveMutation = useMutation({
    mutationFn: async () => {
      const trimmed = name.trim();
      if (!trimmed) throw new Error(t('fileManager.tags.nameRequired'));
      if (editing) {
        return api.updateLibraryTag(editing.id, trimmed);
      }
      return api.createLibraryTag(trimmed);
    },
    onSuccess: () => {
      showToast(t(editing ? 'fileManager.tags.updated' : 'fileManager.tags.created'), 'success');
      setEditorOpen(false);
      setEditing(null);
      setName('');
      invalidate();
    },
    onError: (err: Error) => {
      showToast(err.message || t('fileManager.tags.saveFailed'), 'error');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteLibraryTag(id),
    onSuccess: () => {
      showToast(t('fileManager.tags.deleted'), 'success');
      setDeleteTarget(null);
      invalidate();
    },
    onError: (err: Error) => {
      showToast(err.message || t('fileManager.tags.deleteFailed'), 'error');
    },
  });

  const openCreate = () => {
    setEditing(null);
    setName('');
    setEditorOpen(true);
  };

  const openEdit = (tag: LibraryTag) => {
    setEditing(tag);
    setName(tag.name);
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

  const modalTitleId = 'library-tags-modal-title';
  const editorTitleId = 'library-tag-editor-title';

  return (
    <>
      <Modal
        onClose={onClose}
        size="xl"
        labelledBy={modalTitleId}
        dismissDisabled={
          saveMutation.isPending || deleteMutation.isPending || editorOpen || deleteTarget !== null
        }
      >
        <CardContent className="p-0">
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-bambu-dark-tertiary">
          <div className="min-w-0 flex-1">
            <h2 id={modalTitleId} className="text-lg font-semibold text-white flex items-center gap-2">
              <Tag className="w-5 h-5 text-bambu-green" />
              {t('fileManager.tags.title')}
            </h2>
            <p className="text-bambu-gray text-sm mt-0.5">{t('fileManager.tags.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button onClick={openCreate}>
              <Plus className="w-4 h-4" />
              {t('fileManager.tags.add')}
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
          ) : tags.length === 0 ? (
            <div className="py-16 text-center text-bambu-gray">{t('fileManager.tags.empty')}</div>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary text-left text-bambu-gray">
                  <th className="px-4 py-3 font-medium">{t('fileManager.tags.name')}</th>
                  <th className="px-4 py-3 font-medium text-right">{t('fileManager.tags.fileCount')}</th>
                  <th className="px-4 py-3 font-medium text-right w-32">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {tags.map((tag) => (
                  <tr
                    key={tag.id}
                    className={`border-b border-bambu-dark-tertiary/60 hover:bg-bambu-dark-tertiary/30 ${onPickTag ? 'cursor-pointer' : ''}`}
                    onClick={() => {
                      if (onPickTag) {
                        onPickTag(tag.id);
                        onClose();
                      }
                    }}
                  >
                    <td className="px-4 py-3 text-white font-medium">{tag.name}</td>
                    <td className="px-4 py-3 text-right text-bambu-gray">{tag.file_count}</td>
                    <td className="px-4 py-3 text-right" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center justify-end gap-1">
                        <button
                          type="button"
                          className="p-1.5 text-bambu-gray hover:text-bambu-green rounded"
                          onClick={() => openEdit(tag)}
                          title={t('common.edit')}
                          aria-label={t('fileManager.tags.editAria', { name: tag.name })}
                        >
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          className="p-1.5 text-bambu-gray hover:text-red-400 rounded"
                          onClick={() => setDeleteTarget(tag)}
                          title={t('common.delete')}
                          aria-label={t('fileManager.tags.deleteAria', { name: tag.name })}
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
              {editing ? t('fileManager.tags.edit') : t('fileManager.tags.add')}
            </h3>
            <form onSubmit={handleSave}>
              <label className="block text-sm font-medium text-bambu-gray mb-1" htmlFor="library-tag-name">
                {t('fileManager.tags.name')}
              </label>
              <input
                id="library-tag-name"
                ref={nameInputRef}
                type="text"
                maxLength={64}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:outline-none focus:border-bambu-green mb-4"
                placeholder={t('fileManager.tags.createPlaceholder')}
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
          title={t('fileManager.tags.confirmDelete', { name: deleteTarget.name })}
          message={
            deleteTarget.file_count > 0
              ? t('fileManager.tags.confirmDeleteInUseMessage', { count: deleteTarget.file_count })
              : t('fileManager.tags.confirmDeleteMessage')
          }
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
