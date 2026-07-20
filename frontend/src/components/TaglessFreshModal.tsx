import { useCallback, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ConfirmModal } from './ConfirmModal';
import { api } from '../api/client';
import type { InventorySpool, Printer, TaglessFreshPromptMessage } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { getAmsLabel } from '../utils/amsHelpers';
import { getSwatchStyle } from '../utils/colors';

/**
 * Fresh-roll confirmation for a tagless (non-RFID) slot (W5). Opened from the
 * "Review…" action on the `tagless_fresh_prompt` toast when a tagless roll has
 * been consumed past half its label weight. Confirming ("Fresh roll") archives
 * the current tagless row and mints a replacement; the optional brand / label
 * weight / cost / note ride the new row. Cancelling just drops the modal — the
 * quick "Same roll" answer lives on the toast.
 *
 * Deliberately lean vs `RespoolTagModal`: a tagless roll has no RFID tag, so
 * there is no tag identity, no donor record, and every field is optional.
 */

interface TaglessFreshModalProps {
  /** Slot + spool context; `null` keeps the modal closed. */
  context: TaglessFreshPromptMessage | null;
  /** Called after a successful fresh-roll mint AND when the operator cancels. */
  onClose: () => void;
}

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green';

export function TaglessFreshModal({ context, onClose }: TaglessFreshModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [brand, setBrand] = useState('');
  const [labelWeight, setLabelWeight] = useState('');
  const [costPerKg, setCostPerKg] = useState('');
  const [costTouched, setCostTouched] = useState(false);
  const [note, setNote] = useState('');

  // Known inventory — feeds the brand datalist and the cost-per-kg prefill. Only
  // fetched while the modal is open; the app keeps this cache warm elsewhere.
  const { data: allSpools } = useQuery({
    queryKey: ['inventory-spools'],
    queryFn: () => api.getSpools(true),
    enabled: context != null,
    staleTime: 30_000,
  });

  const brandOptions = Array.from(
    new Set(
      (allSpools ?? [])
        .filter(s => !s.archived_at)
        .map(s => s.brand?.trim())
        .filter((b): b is string => !!b),
    ),
  ).sort((a, b) => a.localeCompare(b));

  const material = context?.material ?? '';

  // Most recent non-archived spool cost/kg for a brand: prefer the same material,
  // else any spool of that brand; blank when there is no prior.
  const suggestCostForBrand = useCallback(
    (brandValue: string): string => {
      const wantBrand = brandValue.trim().toLowerCase();
      if (!wantBrand || !allSpools) return '';
      const wantMaterial = material.trim().toLowerCase();
      const candidates = allSpools.filter(
        (s): s is InventorySpool & { cost_per_kg: number } =>
          !s.archived_at &&
          s.cost_per_kg != null &&
          (s.brand?.trim().toLowerCase() ?? '') === wantBrand,
      );
      if (candidates.length === 0) return '';
      const byRecency = (a: InventorySpool, b: InventorySpool) =>
        (b.created_at ?? '').localeCompare(a.created_at ?? '');
      const sameMaterial = candidates.filter(
        s => (s.material ?? '').trim().toLowerCase() === wantMaterial,
      );
      const pool = sameMaterial.length > 0 ? sameMaterial : candidates;
      pool.sort(byRecency);
      return String(pool[0].cost_per_kg);
    },
    [allSpools, material],
  );

  // Reset the form whenever a different slot's context arrives.
  useEffect(() => {
    if (!context) return;
    setBrand('');
    setLabelWeight('');
    setCostPerKg('');
    setCostTouched(false);
    setNote('');
  }, [context]);

  // Seed / recompute cost from the chosen brand until the operator edits it.
  useEffect(() => {
    if (!context || costTouched) return;
    setCostPerKg(suggestCostForBrand(brand));
  }, [context, brand, costTouched, suggestCostForBrand]);

  const freshMutation = useMutation({
    mutationFn: () => {
      if (!context) throw new Error('no context');
      const weightNum = labelWeight.trim() === '' ? null : Number(labelWeight);
      const costNum = costPerKg.trim() === '' ? null : Number(costPerKg);
      return api.taglessFresh(context.spool_id, {
        printer_id: context.printer_id,
        ams_id: context.ams_id,
        tray_id: context.tray_id,
        answer: 'fresh',
        brand: brand.trim() === '' ? null : brand.trim(),
        label_weight: weightNum != null && Number.isFinite(weightNum) ? weightNum : null,
        cost_per_kg: costNum != null && Number.isFinite(costNum) ? costNum : null,
        note: note.trim() === '' ? null : note.trim(),
      });
    },
    onSuccess: () => {
      showToast(t('inventory.freshRoll.success'), 'success');
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      if (context) {
        queryClient.invalidateQueries({ queryKey: ['printerStatus', context.printer_id] });
      }
      onClose();
    },
    onError: (error: Error) => {
      showToast(error.message || t('inventory.freshRoll.failed'), 'error');
    },
  });

  if (!context) return null;

  const printers = queryClient.getQueryData<Printer[]>(['printers']);
  const printerName = printers?.find(p => p.id === context.printer_id)?.name ?? `Printer ${context.printer_id}`;
  const amsLabel = getAmsLabel(context.ams_id, 4);
  const slotLabel = `${t('inventory.unknownSpoolSlot', 'Slot')} ${context.tray_id + 1}`;
  const location = `${printerName} • ${amsLabel} • ${slotLabel}`;
  const swatchStyle = context.rgba ? getSwatchStyle(context.rgba) : undefined;
  const materialLabel = context.material || '—';

  return (
    <ConfirmModal
      title={t('inventory.freshRoll.title')}
      message={t('inventory.freshRoll.message', { location })}
      confirmText={t('inventory.freshRoll.confirm')}
      cancelText={t('common.cancel')}
      variant="default"
      isLoading={freshMutation.isPending}
      loadingText={t('inventory.freshRoll.pending')}
      onConfirm={() => {
        if (!freshMutation.isPending) freshMutation.mutate();
      }}
      onCancel={onClose}
    >
      <div className="space-y-3">
        {/* Material + colour swatch + remaining grams — the plain-language headline */}
        <div className="flex items-center gap-3 p-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary">
          {swatchStyle && (
            <div
              className="w-8 h-8 rounded-full border border-black/20 flex-shrink-0"
              style={swatchStyle}
              aria-label={context.rgba ?? undefined}
            />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-white text-sm font-medium truncate">{materialLabel}</p>
            <p className="text-xs text-bambu-gray">
              {t('inventory.freshRoll.remaining', { grams: Math.max(0, Math.round(context.remaining_g)) })}
            </p>
          </div>
        </div>

        {/* Brand (optional) — native datalist for zero-typing, free text allowed */}
        <div>
          <label htmlFor="tagless-fresh-brand" className="block text-xs text-bambu-gray mb-1">
            {t('inventory.freshRoll.brandLabel')}
          </label>
          <input
            id="tagless-fresh-brand"
            type="text"
            list="tagless-fresh-brand-options"
            value={brand}
            onChange={e => setBrand(e.target.value)}
            placeholder={t('inventory.freshRoll.brandPlaceholder')}
            className={inputClass}
          />
          <datalist id="tagless-fresh-brand-options">
            {brandOptions.map(b => (
              <option key={b} value={b} />
            ))}
          </datalist>
        </div>

        {/* Label weight + cost/kg (both optional) */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label htmlFor="tagless-fresh-weight" className="block text-xs text-bambu-gray mb-1">
              {t('inventory.freshRoll.weightLabel')}
            </label>
            <input
              id="tagless-fresh-weight"
              type="number"
              min={0}
              value={labelWeight}
              onChange={e => setLabelWeight(e.target.value)}
              placeholder={t('inventory.freshRoll.weightPlaceholder')}
              className={inputClass}
            />
          </div>
          <div className="flex-1">
            <label htmlFor="tagless-fresh-cost" className="block text-xs text-bambu-gray mb-1">
              {t('inventory.freshRoll.costLabel')}
            </label>
            <input
              id="tagless-fresh-cost"
              type="number"
              min={0}
              step={0.01}
              value={costPerKg}
              onChange={e => {
                setCostPerKg(e.target.value);
                setCostTouched(true);
              }}
              placeholder={t('inventory.freshRoll.costPlaceholder')}
              className={inputClass}
            />
          </div>
        </div>

        {/* Optional note */}
        <div>
          <label htmlFor="tagless-fresh-note" className="block text-xs text-bambu-gray mb-1">
            {t('inventory.freshRoll.noteLabel')}
          </label>
          <input
            id="tagless-fresh-note"
            type="text"
            value={note}
            onChange={e => setNote(e.target.value)}
            placeholder={t('inventory.freshRoll.notePlaceholder')}
            className={inputClass}
          />
        </div>
      </div>
    </ConfirmModal>
  );
}
