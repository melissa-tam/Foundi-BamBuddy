/**
 * SKU catalog page (farm production, Phase 2).
 *
 * Full CRUD over SKUs (sellable part codes) plus per-SKU file-link management.
 * In create mode, picking a file in the add-row (or arriving via the
 * post-upload ?createFromFile deep link) auto-suggests the code/part/name from
 * that file's parsed metadata, filling only fields the operator hasn't typed.
 * Each row surfaces lifetime production stats (units completed + success rate)
 * fetched per-SKU.
 *
 * Server state is TanStack Query; numeric link fields are held as strings so
 * they type cleanly and are coerced/validated once on submit. All copy is
 * i18n; inputs are label-linked (WCAG AA) and keyboard operable.
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  Loader2,
  Package,
  Pencil,
  Plus,
  Trash2,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import { usePlateIndexSync } from '../hooks/usePlateIndexSync';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { Modal } from '../components/ui/Modal';
import { FormField, Input, Select, TextArea, inputClass } from '../components/ui/Field';
import { useToast } from '../contexts/ToastContext';
import type { Sku, SkuCreate, SkuFile, SkuFileLinkCreate } from '../types/skus';

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
// Add-row: file + plate + units picker (shared by create and edit dialogs)
// ---------------------------------------------------------------------------

interface SkuLinkAddRowProps {
  /** Controlled selection state, owned by the dialog so Save can commit the
   *  pending link (and, in edit mode, the "Add file" button can read it). */
  fileId: number | null;
  setFileId: (v: number | null) => void;
  plateIndex: number;
  setPlateIndex: (v: number) => void;
  unitsPerPlate: string;
  setUnitsPerPlate: (v: string) => void;
  /** Clears a stale link error when the file selection changes. */
  onClearLinkError: () => void;
  /** Fired with the newly-picked file id (or null) when the file changes. The
   *  create dialog wires this to auto-suggest the SKU fields; edit mode leaves
   *  it undefined so picking a link file never mutates an existing SKU. */
  onFilePicked?: (fileId: number | null) => void;
}

/**
 * The file/plate/units picker. Owns the library-files + plates queries and the
 * plate-index snap so a SINGLE implementation serves both the edit-mode
 * SkuFileLinks panel and the create-mode "Files" section — the latter enabling
 * single-pass SKU creation (pick a file, Save once; the chained
 * createSku→addSkuFile submit commits it).
 */
function SkuLinkAddRow({
  fileId,
  setFileId,
  plateIndex,
  setPlateIndex,
  unitsPerPlate,
  setUnitsPerPlate,
  onClearLinkError,
  onFilePicked,
}: SkuLinkAddRowProps) {
  const { t } = useTranslation();

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

  return (
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
            onFilePicked?.(next);
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
  );
}

// ---------------------------------------------------------------------------
// File-link management (edit mode — links attach to a persisted SKU id)
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
      <SkuLinkAddRow
        fileId={fileId}
        setFileId={setFileId}
        plateIndex={plateIndex}
        setPlateIndex={setPlateIndex}
        unitsPerPlate={unitsPerPlate}
        setUnitsPerPlate={setUnitsPerPlate}
        onClearLinkError={onClearLinkError}
      />
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
  /** Create-mode preseed: a library file id to select in the add-row on open
   *  (from the post-upload "Create SKU from this file" deep link). When set,
   *  the dialog auto-suggests the SKU fields from that file on mount. */
  initialFileId?: number | null;
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

function SkuDialog({ sku, initialFileId, saving, error, linkError, onClearLinkError, onSave, onClose }: SkuDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const titleId = useId();
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

  // Tracks which fields the operator has typed into. Auto-suggest fills ONLY
  // untouched fields so a manual edit is never clobbered; an empty or a
  // previously auto-filled (untouched) field is (re)filled on a new pick.
  const touchedRef = useRef({ code: false, name: false, partNumber: false });

  // Add-row (file/plate/units) selection lives here — above both the file
  // panel that renders the controls and the Save button that must commit them.
  const [linkFileId, setLinkFileId] = useState<number | null>(initialFileId ?? null);
  const [linkPlateIndex, setLinkPlateIndex] = useState<number>(1);
  const [linkUnitsPerPlate, setLinkUnitsPerPlate] = useState<string>('1');

  const { data: ejectProfiles } = useQuery({
    queryKey: ['eject-profiles'],
    queryFn: api.getEjectProfiles,
  });

  const suggestMutation = useMutation({
    mutationFn: (fileId: number) => api.suggestSku(fileId),
    onSuccess: ({ code: sCode, name: sName, part_number: sPart, matched_from }) => {
      // Fill ONLY fields the operator hasn't typed (touchedRef). Empty or
      // previously auto-filled fields are (re)filled; a manual edit is kept.
      let filled = false;
      if (sCode && !touchedRef.current.code) {
        setCode(sCode);
        setCodeError(false);
        filled = true;
      }
      if (sName && !touchedRef.current.name) {
        setName(sName);
        setNameError(false);
        filled = true;
      }
      if (sPart && !touchedRef.current.partNumber) {
        setPartNumber(sPart);
        filled = true;
      }
      if (!filled) return;
      // Map the raw matched_from enum to plain-language copy so no internal
      // token ("object_name"/"filename") ever reaches the operator.
      showToast(
        matched_from === 'object_name'
          ? t('skus.suggest.appliedFromObjectName')
          : matched_from === 'filename'
            ? t('skus.suggest.appliedFromFilename')
            : t('skus.suggest.appliedGeneric'),
      );
    },
    onError: () => {
      // Auto-suggest is best-effort: a failed lookup just leaves the fields for
      // manual entry (the old inline panel error surface is gone with the panel).
      showToast(t('skus.suggest.failed'), 'error');
    },
  });

  // Auto-suggest is CREATE-mode only: picking (or preseeding) a file fills the
  // untouched SKU fields. Editing an existing SKU must NOT auto-mutate its
  // fields, so onFilePicked is never wired in edit mode and this guard holds.
  const handleFilePicked = (fileId: number | null) => {
    if (isEditing) return;
    if (fileId !== null) suggestMutation.mutate(fileId);
  };

  // Preseed path: when the dialog opens via the post-upload deep link
  // (initialFileId set, create mode), run the suggestion once on mount.
  useEffect(() => {
    if (!isEditing && initialFileId != null) {
      suggestMutation.mutate(initialFileId);
    }
    // Mount-only: consume the preseed exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    <Modal onClose={onClose} size="lg" dismissDisabled={saving} labelledBy={titleId}>
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <Package className="w-5 h-5 text-bambu-green" />
              <h2 id={titleId} className="text-lg font-semibold text-white">
                {isEditing ? t('skus.editTitle') : t('skus.createTitle')}
              </h2>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={saving} aria-label={t('common.close')}>
              <X className="w-5 h-5" />
            </Button>
          </div>

          <form onSubmit={handleSubmit} className="p-4 space-y-4">
            {/* Code */}
            <FormField
              id="sku-code"
              label={t('skus.fields.code')}
              error={codeError ? t('skus.codeRequired') : undefined}
            >
              {(field) => (
                <Input
                  {...field}
                  type="text"
                  maxLength={64}
                  value={code}
                  onChange={(e) => {
                    touchedRef.current.code = true;
                    setCode(e.target.value);
                    if (codeError) setCodeError(false);
                  }}
                  className={codeError ? 'border-red-500' : ''}
                  autoFocus
                />
              )}
            </FormField>

            {/* Name */}
            <FormField
              id="sku-name"
              label={t('skus.fields.name')}
              error={nameError ? t('skus.nameRequired') : undefined}
            >
              {(field) => (
                <Input
                  {...field}
                  type="text"
                  maxLength={200}
                  value={name}
                  onChange={(e) => {
                    touchedRef.current.name = true;
                    setName(e.target.value);
                    if (nameError) setNameError(false);
                  }}
                  className={nameError ? 'border-red-500' : ''}
                />
              )}
            </FormField>

            {/* Part number + eject profile */}
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor="sku-part" className="block text-sm text-bambu-gray mb-1">
                  {t('skus.fields.partNumber')}
                </label>
                <Input
                  id="sku-part"
                  type="text"
                  maxLength={64}
                  value={partNumber}
                  onChange={(e) => {
                    touchedRef.current.partNumber = true;
                    setPartNumber(e.target.value);
                  }}
                />
              </div>
              <div>
                <label htmlFor="sku-eject" className="block text-sm text-bambu-gray mb-1">
                  {t('skus.fields.defaultEjectProfile')}
                </label>
                <Select
                  id="sku-eject"
                  value={ejectProfileId ?? ''}
                  onChange={(e) => setEjectProfileId(e.target.value ? Number(e.target.value) : null)}
                >
                  <option value="">{t('skus.fields.noEjectProfile')}</option>
                  {(ejectProfiles ?? []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </Select>
              </div>
            </div>

            {/* Notes */}
            <FormField
              id="sku-notes"
              label={t('skus.fields.notes')}
              labelClassName="block text-sm text-bambu-gray mb-1"
            >
              {(field) => (
                <TextArea
                  {...field}
                  rows={2}
                  maxLength={1000}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                />
              )}
            </FormField>

            {/* File links: edit mode shows the persisted links plus the add
                row; create mode shows the add row alone so a file can be linked
                in a single pass — the chained createSku→addSkuFile submit
                commits it on Save. */}
            {isEditing && sku ? (
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
            ) : (
              <div className="border-t border-bambu-dark-tertiary pt-4">
                <h3 className="text-sm font-semibold text-white mb-2">{t('skus.files.title')}</h3>
                <SkuLinkAddRow
                  fileId={linkFileId}
                  setFileId={setLinkFileId}
                  plateIndex={linkPlateIndex}
                  setPlateIndex={setLinkPlateIndex}
                  unitsPerPlate={linkUnitsPerPlate}
                  setUnitsPerPlate={setLinkUnitsPerPlate}
                  onClearLinkError={onClearLinkError}
                  onFilePicked={handleFilePicked}
                />
                <p className="text-xs text-bambu-gray mt-2">{t('skus.files.willLinkOnSave')}</p>
              </div>
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
          </form>
        </CardContent>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function SkusPage() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();

  const [dialogState, setDialogState] = useState<{ open: boolean; sku: Sku | null }>({
    open: false,
    sku: null,
  });
  const [pendingDelete, setPendingDelete] = useState<Sku | null>(null);
  // Set only when a save PERSISTED the SKU but its chained file link failed;
  // rendered inline in the file panel while the dialog stays open in edit mode.
  const [linkError, setLinkError] = useState<string | null>(null);
  // Create-mode preseed file id, set from the ?createFromFile deep link so the
  // dialog opens with that file in the add-row + auto-suggest fired. Cleared
  // whenever a dialog is opened another way, so it never leaks into a fresh one.
  const [createFromFileId, setCreateFromFileId] = useState<number | null>(null);

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
    // A dialog opened via the toolbar/edit buttons has no preseed file.
    setCreateFromFileId(null);
    setDialogState({ open: true, sku });
  };

  // Deep-link entry: the post-upload "Create SKU from this file" toast navigates
  // here with ?createFromFile=<id>. Open the create dialog with that file
  // preseeded (the dialog auto-suggests from it on mount), then strip the param
  // via replace so a refresh or Back doesn't reopen the dialog.
  useEffect(() => {
    const raw = searchParams.get('createFromFile');
    if (!raw) return;
    const fileId = Number(raw);
    if (!Number.isInteger(fileId) || fileId <= 0) return;
    saveMutation.reset();
    setLinkError(null);
    setCreateFromFileId(fileId);
    setDialogState({ open: true, sku: null });
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('createFromFile');
        return next;
      },
      { replace: true },
    );
    // Mount-only: consume the deep-link param exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
          initialFileId={createFromFileId}
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
            setCreateFromFileId(null);
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
