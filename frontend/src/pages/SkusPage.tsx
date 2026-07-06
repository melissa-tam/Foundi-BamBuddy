/**
 * SKU catalog page (farm production, Phase 2).
 *
 * Full CRUD over SKUs (sellable part codes) plus per-SKU file-link management
 * and a "Suggest from file" flow that pre-fills the code/part/name from a
 * library file's parsed metadata. Each row surfaces lifetime production stats
 * (units completed + success rate) fetched per-SKU.
 *
 * Server state is TanStack Query; numeric link fields are held as strings so
 * they type cleanly and are coerced/validated once on submit. All copy is
 * i18n; inputs are label-linked (WCAG AA) and keyboard operable.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  Loader2,
  Package,
  Pencil,
  Plus,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import { usePlateIndexSync } from '../hooks/usePlateIndexSync';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import type { Sku, SkuCreate, SkuFile, SkuFileLinkCreate } from '../types/skus';

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary ' +
  'focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

/**
 * Build the POST /skus/{id}/files payload from the raw add-row inputs. Single
 * source of the units coercion so the "Add file" button and a Save-committed
 * link produce byte-identical bodies through the same `api.addSkuFile` call.
 */
function buildLinkPayload(
  fileId: number,
  plateIndex: number,
  unitsPerPlate: string,
): SkuFileLinkCreate {
  const units = Number(unitsPerPlate);
  return {
    library_file_id: fileId,
    plate_index: plateIndex,
    units_per_plate: Number.isFinite(units) && units >= 1 ? units : 1,
  };
}

// ---------------------------------------------------------------------------
// Lifetime stats cell (one query per SKU row)
// ---------------------------------------------------------------------------

function SkuStatsCells({ skuId }: { skuId: number }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['sku-stats', skuId],
    queryFn: () => api.getSkuStats(skuId),
  });

  if (isLoading) {
    return (
      <>
        <td className="py-3 px-4 text-bambu-gray text-right">
          <Loader2 className="w-4 h-4 animate-spin inline" />
        </td>
        <td className="py-3 px-4 text-bambu-gray text-right">—</td>
      </>
    );
  }
  if (isError || !data) {
    return (
      <>
        <td className="py-3 px-4 text-bambu-gray text-right">—</td>
        <td className="py-3 px-4 text-bambu-gray text-right">—</td>
      </>
    );
  }

  const rate = data.success_rate == null ? null : Math.round(data.success_rate * 100);
  return (
    <>
      <td className="py-3 px-4 text-bambu-gray text-right tabular-nums">{data.units_completed}</td>
      <td className="py-3 px-4 text-bambu-gray text-right tabular-nums">
        {rate == null ? '—' : `${rate}%`}
      </td>
    </>
  );
}

// ---------------------------------------------------------------------------
// File-link management (edit mode only — links attach to a persisted SKU id)
// ---------------------------------------------------------------------------

interface SkuFileLinksProps {
  sku: Sku;
  /** Add-row selection state, lifted to the dialog so Save can read/commit the
   *  pending link. This component drives it through the setters below. */
  fileId: number | null;
  setFileId: (v: number | null) => void;
  plateIndex: number;
  setPlateIndex: (v: number) => void;
  unitsPerPlate: string;
  setUnitsPerPlate: (v: string) => void;
  /** Inline error when Save persisted the SKU but its pending link failed;
   *  rendered in the same region as the "Add file" error. */
  saveLinkError: string | null;
  onClearLinkError: () => void;
}

function SkuFileLinks({
  sku,
  fileId,
  setFileId,
  plateIndex,
  setPlateIndex,
  unitsPerPlate,
  setUnitsPerPlate,
  saveLinkError,
  onClearLinkError,
}: SkuFileLinksProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [pendingRemove, setPendingRemove] = useState<SkuFile | null>(null);

  // The dialog is seeded with the list row, then kept live via a detail query
  // so file mutations reflect immediately without closing the dialog.
  const { data: detail } = useQuery({
    queryKey: ['sku', sku.id],
    queryFn: () => api.getSku(sku.id),
    initialData: sku,
  });
  const files = detail?.files ?? [];

  const { data: libraryFiles } = useQuery({
    queryKey: ['library-files', 'sku'],
    queryFn: () => api.getLibraryFiles(),
  });
  const threeMfFiles = useMemo(
    () => (libraryFiles ?? []).filter((f) => f.filename.toLowerCase().endsWith('.3mf')),
    [libraryFiles],
  );

  const { data: platesData } = useQuery({
    queryKey: ['library-file-plates', fileId],
    queryFn: () => api.getLibraryFilePlates(fileId!),
    enabled: fileId !== null,
  });
  const plates = useMemo(() => platesData?.plates ?? [], [platesData]);
  usePlateIndexSync(plates, plateIndex, setPlateIndex);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['sku', sku.id] });
    queryClient.invalidateQueries({ queryKey: ['skus'] });
  };

  const addMutation = useMutation({
    mutationFn: () => api.addSkuFile(sku.id, buildLinkPayload(fileId!, plateIndex, unitsPerPlate)),
    onSuccess: () => {
      showToast(t('skus.files.added'));
      invalidate();
      setFileId(null);
      setPlateIndex(1);
      setUnitsPerPlate('1');
      onClearLinkError();
    },
    // No error toast: the failure renders inline under the add controls.
  });

  const removeMutation = useMutation({
    mutationFn: (linkId: number) => api.deleteSkuFile(sku.id, linkId),
    onSuccess: () => {
      showToast(t('skus.files.removed'));
      invalidate();
      setPendingRemove(null);
    },
    onError: () => {
      // Close the confirm; the failure detail renders inline in the section.
      setPendingRemove(null);
    },
  });

  const canAdd = fileId !== null && !addMutation.isPending;

  return (
    <div className="border-t border-bambu-dark-tertiary pt-4">
      <h3 className="text-sm font-semibold text-white mb-2">{t('skus.files.title')}</h3>

      {files.length === 0 ? (
        <p className="text-sm text-bambu-gray italic mb-3">{t('skus.files.empty')}</p>
      ) : (
        <ul className="space-y-2 mb-3">
          {files.map((f) => (
            <li
              key={f.id}
              className="flex items-center justify-between gap-2 p-2 bg-bambu-dark rounded-md text-sm"
            >
              <div className="min-w-0">
                <span className="text-white truncate block">{f.library_file_name}</span>
                <span className="text-bambu-gray text-xs">
                  {t('skus.files.plateUnits', {
                    plate: f.plate_index,
                    units: f.units_per_plate,
                  })}
                  {f.printer_model ? ` · ${f.printer_model}` : ''}
                </span>
              </div>
              <button
                type="button"
                onClick={() => setPendingRemove(f)}
                disabled={removeMutation.isPending}
                className="p-2 rounded text-red-400 hover:text-red-300 hover:bg-bambu-dark-tertiary transition-colors focus:outline-none focus:ring-2 focus:ring-red-500/50 disabled:opacity-50"
                aria-label={`${t('skus.files.remove')} ${f.library_file_name}`}
                title={t('skus.files.remove')}
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Add-link sub-form */}
      <div className="grid gap-2 sm:grid-cols-3">
        <div>
          <label htmlFor="sku-link-file" className="block text-xs text-bambu-gray mb-1">
            {t('skus.files.file')}
          </label>
          <select
            id="sku-link-file"
            value={fileId ?? ''}
            onChange={(e) => {
              const next = e.target.value ? Number(e.target.value) : null;
              setFileId(next);
              setPlateIndex(1);
              onClearLinkError();
            }}
            className={inputClass}
          >
            <option value="">{t('skus.files.filePlaceholder')}</option>
            {threeMfFiles.map((f) => (
              <option key={f.id} value={f.id}>
                {f.filename}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="sku-link-plate" className="block text-xs text-bambu-gray mb-1">
            {t('skus.files.plate')}
          </label>
          <select
            id="sku-link-plate"
            value={plateIndex}
            onChange={(e) => setPlateIndex(Number(e.target.value))}
            className={inputClass}
            disabled={fileId === null}
          >
            {plates.length > 0 ? (
              plates.map((p) => (
                <option key={p.index} value={p.index}>
                  {p.name || `#${p.index}`}
                </option>
              ))
            ) : (
              <option value={1}>#1</option>
            )}
          </select>
        </div>
        <div>
          <label htmlFor="sku-link-units" className="block text-xs text-bambu-gray mb-1">
            {t('skus.files.unitsPerPlate')}
          </label>
          <input
            id="sku-link-units"
            type="number"
            inputMode="numeric"
            min={1}
            max={999}
            step={1}
            value={unitsPerPlate}
            onChange={(e) => setUnitsPerPlate(e.target.value)}
            className={inputClass}
          />
        </div>
      </div>
      <div className="mt-2">
        <Button type="button" variant="secondary" size="sm" onClick={() => addMutation.mutate()} disabled={!canAdd}>
          {addMutation.isPending ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Plus className="w-4 h-4" />
          )}
          {t('skus.files.add')}
        </Button>
        {addMutation.error && (
          <p role="alert" className="text-red-400 text-xs mt-2">
            {addMutation.error.message || t('skus.files.addFailed')}
          </p>
        )}
        {saveLinkError && (
          <p role="alert" className="text-red-400 text-xs mt-2">
            {saveLinkError}
          </p>
        )}
        {removeMutation.error && (
          <p role="alert" className="text-red-400 text-xs mt-2">
            {removeMutation.error.message || t('skus.files.removeFailed')}
          </p>
        )}
      </div>

      {pendingRemove && (
        <ConfirmModal
          title={t('skus.files.removeTitle')}
          message={t('skus.files.removeBody', { name: pendingRemove.library_file_name })}
          confirmText={t('skus.files.remove')}
          cancelText={t('common.cancel')}
          variant="danger"
          overlayZIndex="z-[110]"
          isLoading={removeMutation.isPending}
          onConfirm={() => removeMutation.mutate(pendingRemove.id)}
          onCancel={() => setPendingRemove(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create / edit dialog
// ---------------------------------------------------------------------------

interface SkuDialogProps {
  sku: Sku | null;
  saving: boolean;
  /** Backend failure detail from the last save attempt (e.g. 409 duplicate
   *  code); rendered inline so a rejected save never dead-ends silently. */
  error: string | null;
  /** Inline detail when Save persisted the SKU but its pending file link
   *  failed; shown inside the file panel, not as a top-level save error. */
  linkError: string | null;
  onClearLinkError: () => void;
  /** Commits the SKU fields and, when the add-row has a selection, the pending
   *  file link too — so Save never silently drops a chosen file. */
  onSave: (data: SkuCreate, pendingLink: SkuFileLinkCreate | null) => void;
  onClose: () => void;
}

function SkuDialog({ sku, saving, error, linkError, onClearLinkError, onSave, onClose }: SkuDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const isEditing = sku !== null;

  const [code, setCode] = useState(sku?.code ?? '');
  const [name, setName] = useState(sku?.name ?? '');
  const [partNumber, setPartNumber] = useState(sku?.part_number ?? '');
  const [notes, setNotes] = useState(sku?.notes ?? '');
  const [ejectProfileId, setEjectProfileId] = useState<number | null>(
    sku?.default_eject_profile_id ?? null,
  );
  const [codeError, setCodeError] = useState(false);
  const [nameError, setNameError] = useState(false);
  const [suggestFileId, setSuggestFileId] = useState<number | null>(null);

  // Add-row (file/plate/units) selection lives here — above both the file
  // panel that renders the controls and the Save button that must commit them.
  const [linkFileId, setLinkFileId] = useState<number | null>(null);
  const [linkPlateIndex, setLinkPlateIndex] = useState<number>(1);
  const [linkUnitsPerPlate, setLinkUnitsPerPlate] = useState<string>('1');

  const { data: ejectProfiles } = useQuery({
    queryKey: ['eject-profiles'],
    queryFn: api.getEjectProfiles,
  });

  const { data: libraryFiles } = useQuery({
    queryKey: ['library-files', 'sku'],
    queryFn: () => api.getLibraryFiles(),
  });
  const threeMfFiles = useMemo(
    () => (libraryFiles ?? []).filter((f) => f.filename.toLowerCase().endsWith('.3mf')),
    [libraryFiles],
  );

  const suggestMutation = useMutation({
    mutationFn: (fileId: number) => api.suggestSku(fileId),
    onSuccess: (s) => {
      if (s.code) setCode(s.code);
      if (s.part_number) setPartNumber(s.part_number);
      if (s.name) setName(s.name);
      if (s.code) setCodeError(false);
      if (s.name) setNameError(false);
      showToast(
        s.matched_from
          ? t('skus.suggest.applied', { source: s.matched_from })
          : t('skus.suggest.appliedGeneric'),
      );
    },
    // No error toast: the failure renders inline next to the suggest control.
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    let invalid = false;
    if (!code.trim()) {
      setCodeError(true);
      invalid = true;
    }
    if (!name.trim()) {
      setNameError(true);
      invalid = true;
    }
    if (invalid) return;
    // If the operator picked a file in the add-row but never pressed "Add
    // file", Save commits it too (chained after the SKU persists). An untouched
    // add-row yields null → Save behaves exactly as before.
    const pendingLink =
      linkFileId !== null
        ? buildLinkPayload(linkFileId, linkPlateIndex, linkUnitsPerPlate)
        : null;
    onSave(
      {
        code: code.trim(),
        name: name.trim(),
        part_number: partNumber.trim() || null,
        notes: notes.trim() || null,
        default_eject_profile_id: ejectProfileId,
      },
      pendingLink,
    );
  };

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={saving ? undefined : onClose}
      role="dialog"
      aria-modal="true"
      aria-label={isEditing ? t('skus.editTitle') : t('skus.createTitle')}
    >
      <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <Package className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">
                {isEditing ? t('skus.editTitle') : t('skus.createTitle')}
              </h2>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={saving} aria-label={t('common.close')}>
              <X className="w-5 h-5" />
            </Button>
          </div>

          <form onSubmit={handleSubmit} className="p-4 space-y-4">
            {/* Suggest from file */}
            <div className="p-3 bg-bambu-dark rounded-lg">
              <div className="flex items-center gap-2 mb-2">
                <Sparkles className="w-4 h-4 text-bambu-green" />
                <span className="text-sm font-medium text-white">{t('skus.suggest.title')}</span>
              </div>
              <p className="text-xs text-bambu-gray mb-2">{t('skus.suggest.description')}</p>
              <div className="flex gap-2">
                <label htmlFor="sku-suggest-file" className="sr-only">
                  {t('skus.suggest.file')}
                </label>
                <select
                  id="sku-suggest-file"
                  value={suggestFileId ?? ''}
                  onChange={(e) => setSuggestFileId(e.target.value ? Number(e.target.value) : null)}
                  className={inputClass}
                >
                  <option value="">{t('skus.suggest.filePlaceholder')}</option>
                  {threeMfFiles.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.filename}
                    </option>
                  ))}
                </select>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => suggestFileId !== null && suggestMutation.mutate(suggestFileId)}
                  disabled={suggestFileId === null || suggestMutation.isPending}
                >
                  {suggestMutation.isPending ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <Sparkles className="w-4 h-4" />
                  )}
                  {t('skus.suggest.action')}
                </Button>
              </div>
              {suggestMutation.error && (
                <p role="alert" className="text-red-400 text-xs mt-2">
                  {suggestMutation.error.message || t('skus.suggest.failed')}
                </p>
              )}
            </div>

            {/* Code */}
            <div>
              <label htmlFor="sku-code" className="block text-sm font-medium text-white mb-1">
                {t('skus.fields.code')}
              </label>
              <input
                id="sku-code"
                type="text"
                maxLength={64}
                value={code}
                onChange={(e) => {
                  setCode(e.target.value);
                  if (codeError) setCodeError(false);
                }}
                className={`${inputClass} ${codeError ? 'border-red-500' : ''}`}
                aria-invalid={codeError}
                aria-describedby={codeError ? 'sku-code-error' : undefined}
                autoFocus
              />
              {codeError && (
                <p id="sku-code-error" className="text-red-400 text-xs mt-1">
                  {t('skus.codeRequired')}
                </p>
              )}
            </div>

            {/* Name */}
            <div>
              <label htmlFor="sku-name" className="block text-sm font-medium text-white mb-1">
                {t('skus.fields.name')}
              </label>
              <input
                id="sku-name"
                type="text"
                maxLength={200}
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  if (nameError) setNameError(false);
                }}
                className={`${inputClass} ${nameError ? 'border-red-500' : ''}`}
                aria-invalid={nameError}
                aria-describedby={nameError ? 'sku-name-error' : undefined}
              />
              {nameError && (
                <p id="sku-name-error" className="text-red-400 text-xs mt-1">
                  {t('skus.nameRequired')}
                </p>
              )}
            </div>

            {/* Part number + eject profile */}
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor="sku-part" className="block text-sm text-bambu-gray mb-1">
                  {t('skus.fields.partNumber')}
                </label>
                <input
                  id="sku-part"
                  type="text"
                  maxLength={64}
                  value={partNumber}
                  onChange={(e) => setPartNumber(e.target.value)}
                  className={inputClass}
                />
              </div>
              <div>
                <label htmlFor="sku-eject" className="block text-sm text-bambu-gray mb-1">
                  {t('skus.fields.defaultEjectProfile')}
                </label>
                <select
                  id="sku-eject"
                  value={ejectProfileId ?? ''}
                  onChange={(e) => setEjectProfileId(e.target.value ? Number(e.target.value) : null)}
                  className={inputClass}
                >
                  <option value="">{t('skus.fields.noEjectProfile')}</option>
                  {(ejectProfiles ?? []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Notes */}
            <div>
              <label htmlFor="sku-notes" className="block text-sm text-bambu-gray mb-1">
                {t('skus.fields.notes')}
              </label>
              <textarea
                id="sku-notes"
                rows={2}
                maxLength={1000}
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                className={inputClass}
              />
            </div>

            {/* File links (edit only) */}
            {isEditing && sku && (
              <SkuFileLinks
                sku={sku}
                fileId={linkFileId}
                setFileId={setLinkFileId}
                plateIndex={linkPlateIndex}
                setPlateIndex={setLinkPlateIndex}
                unitsPerPlate={linkUnitsPerPlate}
                setUnitsPerPlate={setLinkUnitsPerPlate}
                saveLinkError={linkError}
                onClearLinkError={onClearLinkError}
              />
            )}

            {/* Backend rejection (persistent, unlike a toast — the dialog is
                the surface the user is looking at when the save fails). */}
            {error && (
              <div
                role="alert"
                className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300"
              >
                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
                <span>{error}</span>
              </div>
            )}

            {/* Actions */}
            <div className="flex gap-3 pt-2">
              <Button type="button" variant="secondary" onClick={onClose} className="flex-1" disabled={saving}>
                {t('common.cancel')}
              </Button>
              <Button type="submit" className="flex-1" disabled={saving}>
                {saving ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    {t('common.saving')}
                  </>
                ) : (
                  <>
                    <Package className="w-4 h-4" />
                    {t('common.save')}
                  </>
                )}
              </Button>
            </div>
            {!isEditing && (
              <p className="text-xs text-bambu-gray">{t('skus.files.createFirst')}</p>
            )}
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function SkusPage() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [dialogState, setDialogState] = useState<{ open: boolean; sku: Sku | null }>({
    open: false,
    sku: null,
  });
  const [pendingDelete, setPendingDelete] = useState<Sku | null>(null);
  // Set only when a save PERSISTED the SKU but its chained file link failed;
  // rendered inline in the file panel while the dialog stays open in edit mode.
  const [linkError, setLinkError] = useState<string | null>(null);

  const { data: skus, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['skus'],
    queryFn: api.getSkus,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['skus'] });

  const saveMutation = useMutation({
    // Save is the primary action, so it commits the operator's full intent:
    // the SKU fields AND — when the add-row has a pending selection — the file
    // link, chained through the same `api.addSkuFile` call as the "Add file"
    // button. Order matters: the SKU must persist before its link can attach.
    mutationFn: async ({
      data,
      pendingLink,
    }: {
      data: SkuCreate;
      pendingLink: SkuFileLinkCreate | null;
    }) => {
      const editingId = dialogState.sku?.id;
      const saved = editingId != null ? await api.updateSku(editingId, data) : await api.createSku(data);
      let linkFailed: string | null = null;
      if (pendingLink) {
        // The SKU is already persisted — a link failure (e.g. unsliced plate →
        // 400) must NOT roll it back or masquerade as a save failure. Capture
        // it so the dialog can stay open and surface it honestly.
        try {
          await api.addSkuFile(saved.id, pendingLink);
        } catch (err) {
          linkFailed = (err as Error).message || t('skus.files.addFailed');
        }
      }
      return { saved, wasEditing: editingId != null, hadLink: pendingLink != null, linkFailed };
    },
    onSuccess: ({ saved, wasEditing, hadLink, linkFailed }) => {
      showToast(wasEditing ? t('skus.updated') : t('skus.created'));
      invalidate();
      if (linkFailed) {
        // SKU saved, link didn't: keep the dialog open in edit mode for the
        // saved SKU and show the link error inline so the operator never exits
        // believing the file was linked.
        setLinkError(t('skus.files.savedButLinkFailed', { reason: linkFailed }));
        setDialogState({ open: true, sku: saved });
        return;
      }
      // Full success. Close when a link was committed via Save or on any edit
      // save; on a plain create with no pending link, keep the dialog open so
      // the operator can attach files to the freshly-minted SKU.
      setDialogState(hadLink || wasEditing ? { open: false, sku: null } : { open: true, sku: saved });
    },
    // No error toast: a create/update failure (e.g. 409 duplicate code) renders
    // inline inside the open dialog (SkuDialog `error` prop).
  });

  const openDialog = (sku: Sku | null) => {
    // Clear any error left over from a previous failed attempt so the dialog
    // opens clean.
    saveMutation.reset();
    setLinkError(null);
    setDialogState({ open: true, sku });
  };

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteSku(id),
    onSuccess: () => {
      showToast(t('skus.deleted'));
      invalidate();
      setPendingDelete(null);
    },
    onError: (err: Error) => {
      showToast(err.message || t('skus.deleteFailed'), 'error');
      setPendingDelete(null);
    },
  });

  const list = skus ?? [];

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <Package className="w-6 h-6 text-bambu-green" />
            {t('skus.title')}
          </h1>
          <p className="text-sm text-bambu-gray mt-1 max-w-2xl">{t('skus.description')}</p>
        </div>
        {list.length > 0 && (
          <Button onClick={() => openDialog(null)}>
            <Plus className="w-4 h-4" />
            {t('skus.newSku')}
          </Button>
        )}
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-16 text-bambu-gray" role="status">
          <Loader2 className="w-6 h-6 animate-spin" />
          <span className="ml-2">{t('common.loading')}</span>
        </div>
      ) : isError ? (
        <Card>
          <CardContent className="flex flex-col items-center text-center py-12">
            <AlertCircle className="w-10 h-10 text-red-400 mb-3" />
            <p className="text-white mb-4">{t('skus.loadError')}</p>
            <Button variant="secondary" onClick={() => refetch()} disabled={isFetching}>
              {isFetching ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              {t('common.retry')}
            </Button>
          </CardContent>
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center text-center py-16">
            <Package className="w-12 h-12 text-bambu-gray mb-4" />
            <h2 className="text-lg font-semibold text-white mb-1">{t('skus.empty.title')}</h2>
            <p className="text-sm text-bambu-gray mb-5 max-w-md">{t('skus.empty.body')}</p>
            <Button onClick={() => openDialog(null)}>
              <Plus className="w-4 h-4" />
              {t('skus.newSku')}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-bambu-gray text-left border-b border-bambu-dark-tertiary">
                <tr>
                  <th className="py-3 px-4 font-medium">{t('skus.fields.code')}</th>
                  <th className="py-3 px-4 font-medium">{t('skus.fields.name')}</th>
                  <th className="py-3 px-4 font-medium">{t('skus.fields.partNumber')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('skus.columns.files')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('skus.columns.unitsCompleted')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('skus.columns.successRate')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => (
                  <tr key={s.id} className="border-b border-bambu-dark-tertiary last:border-b-0">
                    <td className="py-3 px-4 text-white font-medium">{s.code}</td>
                    <td className="py-3 px-4 text-bambu-gray">{s.name}</td>
                    <td className="py-3 px-4 text-bambu-gray">{s.part_number || '—'}</td>
                    <td className="py-3 px-4 text-bambu-gray text-right tabular-nums">{s.files.length}</td>
                    <SkuStatsCells skuId={s.id} />
                    <td className="py-3 px-4">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          type="button"
                          onClick={() => openDialog(s)}
                          className="p-2 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50"
                          aria-label={`${t('common.edit')} ${s.code}`}
                          title={t('common.edit')}
                        >
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setPendingDelete(s)}
                          className="p-2 rounded text-red-400 hover:text-red-300 hover:bg-bambu-dark-tertiary transition-colors focus:outline-none focus:ring-2 focus:ring-red-500/50"
                          aria-label={`${t('common.delete')} ${s.code}`}
                          title={t('common.delete')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {dialogState.open && (
        <SkuDialog
          sku={dialogState.sku}
          saving={saveMutation.isPending}
          error={
            saveMutation.error ? saveMutation.error.message || t('skus.saveFailed') : null
          }
          linkError={linkError}
          onClearLinkError={() => setLinkError(null)}
          onSave={(data, pendingLink) => {
            setLinkError(null);
            saveMutation.mutate({ data, pendingLink });
          }}
          onClose={() => {
            setLinkError(null);
            setDialogState({ open: false, sku: null });
          }}
        />
      )}

      {pendingDelete && (
        <ConfirmModal
          title={t('skus.deleteTitle')}
          message={t('skus.deleteBody', { code: pendingDelete.code })}
          confirmText={t('common.delete')}
          cancelText={t('common.cancel')}
          variant="danger"
          isLoading={deleteMutation.isPending}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}

export default SkusPage;
