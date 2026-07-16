import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import { ConfirmModal } from './ConfirmModal';
import { api } from '../api/client';
import type { Printer, RespoolPromptMessage, RespoolRequest } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { getAmsLabel } from '../utils/amsHelpers';
import { getSwatchStyle } from '../utils/colors';

// Client-side fallback for the brand prefill when the backend didn't supply one
// (the manual tray-menu path, or the very first re-spool before a brand is set).
// Fleet refills come from one supplier batch, so the last brand is the best
// guess. The authoritative prefill is the server-held `respool_last_brand`,
// delivered as `brand_prefill` on the WS prompt payload.
const LAST_BRAND_KEY = 'respool_last_brand';

function readLastBrand(): string {
  try {
    return window.localStorage.getItem(LAST_BRAND_KEY) ?? '';
  } catch {
    return '';
  }
}

function writeLastBrand(brand: string): void {
  try {
    window.localStorage.setItem(LAST_BRAND_KEY, brand);
  } catch {
    /* localStorage unavailable (private mode / quota) — prefill is a
       convenience only, so silently skip. */
  }
}

interface RespoolTagModalProps {
  /** Slot + prefill context; ``null`` keeps the modal closed. Supplied by the
   *  global prompt queue (`useRespoolPrompt`) or the manual tray-menu action. */
  context: RespoolPromptMessage | null;
  /** Called after a successful re-spool AND when the operator dismisses. */
  onClose: () => void;
}

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green';

export function RespoolTagModal({ context, onClose }: RespoolTagModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [brand, setBrand] = useState('');
  const [labelWeight, setLabelWeight] = useState('');
  const [costPerKg, setCostPerKg] = useState('');
  const [note, setNote] = useState('');

  // Reset the form whenever a different slot's context arrives (queue advances
  // or a new manual open). `context` is a stable object per queue entry, so
  // this doesn't fire on unrelated parent re-renders.
  useEffect(() => {
    if (!context) return;
    setBrand(context.brand_prefill ?? readLastBrand());
    setLabelWeight(context.label_weight_prefill != null ? String(context.label_weight_prefill) : '');
    setCostPerKg('');
    setNote('');
  }, [context]);

  const respoolMutation = useMutation({
    mutationFn: (payload: RespoolRequest) => api.respoolTag(payload),
    onSuccess: (spool) => {
      writeLastBrand(brand.trim());
      showToast(t('inventory.respool.success', { brand: spool.brand ?? brand.trim() }), 'success');
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      if (context) {
        queryClient.invalidateQueries({ queryKey: ['printerStatus', context.printer_id] });
      }
      onClose();
    },
    onError: (error: Error) => {
      // Surface the backend's user-actionable detail (409 sibling-tag conflict /
      // spoolman mode, 400 empty slot / no tag, 404 printer offline).
      showToast(error.message || t('inventory.respool.failed'), 'error');
    },
  });

  if (!context) return null;

  const trayCount = context.tray_count ?? 4;
  const printers = queryClient.getQueryData<Printer[]>(['printers']);
  const printerName = printers?.find(p => p.id === context.printer_id)?.name ?? `Printer ${context.printer_id}`;
  const amsLabel = getAmsLabel(context.ams_id, trayCount);
  const slotLabel = `${t('inventory.unknownSpoolSlot', 'Slot')} ${context.tray_id + 1}`;
  const location = `${printerName} • ${amsLabel} • ${slotLabel}`;

  const material = context.tray_sub_brands || context.tray_type || '—';
  const swatchStyle = context.tray_color ? getSwatchStyle(context.tray_color) : undefined;
  const tagIdentity = context.tag_uid || context.tray_uuid || null;

  const brandValid = brand.trim().length > 0;

  const handleConfirm = () => {
    if (!brandValid || respoolMutation.isPending) return;
    const weightNum = labelWeight.trim() === '' ? null : Number(labelWeight);
    const costNum = costPerKg.trim() === '' ? null : Number(costPerKg);
    respoolMutation.mutate({
      printer_id: context.printer_id,
      ams_id: context.ams_id,
      tray_id: context.tray_id,
      brand: brand.trim(),
      label_weight: weightNum != null && Number.isFinite(weightNum) ? weightNum : null,
      cost_per_kg: costNum != null && Number.isFinite(costNum) ? costNum : null,
      note: note.trim() === '' ? null : note.trim(),
    });
  };

  return (
    <ConfirmModal
      title={t('inventory.respool.title')}
      message={t('inventory.respool.message', { location })}
      confirmText={t('inventory.respool.confirm')}
      cancelText={t('inventory.respool.dismiss')}
      variant="default"
      isLoading={respoolMutation.isPending}
      loadingText={t('inventory.respool.pending')}
      confirmDisabled={!brandValid}
      onConfirm={handleConfirm}
      onCancel={onClose}
    >
      <div className="space-y-3">
        {/* Material + colour swatch + tag identity */}
        <div className="flex items-center gap-3 p-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary">
          {swatchStyle && (
            <div
              className="w-8 h-8 rounded-full border border-black/20 flex-shrink-0"
              style={swatchStyle}
              aria-label={context.tray_color ?? undefined}
            />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-white text-sm font-medium truncate">{material}</p>
            {tagIdentity && (
              <p className="text-xs text-bambu-gray font-mono truncate">{tagIdentity}</p>
            )}
          </div>
        </div>

        {/* Donor spool line — only when a Bambuddy row backs this tag */}
        {context.donor_spool_id != null && (
          <p className="text-xs text-bambu-gray">
            {t('inventory.respool.donorLine', {
              id: context.donor_spool_id,
              grams: Math.max(0, Math.round(context.donor_remaining_g ?? 0)),
            })}
          </p>
        )}

        {/* Persistent one-tag-per-roll warning */}
        <div
          role="note"
          className="flex items-start gap-2 p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/40 text-yellow-200 text-xs"
        >
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" aria-hidden="true" />
          <span>{t('inventory.respool.warning')}</span>
        </div>

        {/* Brand (required) */}
        <div>
          <label htmlFor="respool-brand" className="block text-xs text-bambu-gray mb-1">
            {t('inventory.respool.brandLabel')}
          </label>
          <input
            id="respool-brand"
            type="text"
            value={brand}
            onChange={e => setBrand(e.target.value)}
            aria-required="true"
            placeholder={t('inventory.respool.brandPlaceholder')}
            className={inputClass}
          />
        </div>

        {/* Label weight + cost/kg */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label htmlFor="respool-weight" className="block text-xs text-bambu-gray mb-1">
              {t('inventory.respool.weightLabel')}
            </label>
            <input
              id="respool-weight"
              type="number"
              min={0}
              value={labelWeight}
              onChange={e => setLabelWeight(e.target.value)}
              placeholder={t('inventory.respool.weightPlaceholder')}
              className={inputClass}
            />
          </div>
          <div className="flex-1">
            <label htmlFor="respool-cost" className="block text-xs text-bambu-gray mb-1">
              {t('inventory.respool.costLabel')}
            </label>
            <input
              id="respool-cost"
              type="number"
              min={0}
              step={0.01}
              value={costPerKg}
              onChange={e => setCostPerKg(e.target.value)}
              placeholder={t('inventory.respool.costPlaceholder')}
              className={inputClass}
            />
          </div>
        </div>

        {/* Optional note */}
        <div>
          <label htmlFor="respool-note" className="block text-xs text-bambu-gray mb-1">
            {t('inventory.respool.noteLabel')}
          </label>
          <input
            id="respool-note"
            type="text"
            value={note}
            onChange={e => setNote(e.target.value)}
            placeholder={t('inventory.respool.notePlaceholder')}
            className={inputClass}
          />
        </div>
      </div>
    </ConfirmModal>
  );
}
