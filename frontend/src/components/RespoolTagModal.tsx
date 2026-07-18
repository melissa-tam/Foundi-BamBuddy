import { useCallback, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import { ConfirmModal } from './ConfirmModal';
import { api } from '../api/client';
import type { InventorySpool, Printer, RespoolPromptMessage, RespoolRequest } from '../api/client';
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

// The `tagless_default_filament` setting is a JSON string ({brand, material, …}).
// Parse defensively — malformed / absent → no brand.
function parseTaglessBrand(raw: string | null | undefined): string {
  if (!raw) return '';
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const brand = (parsed as { brand?: unknown }).brand;
      if (typeof brand === 'string') return brand;
    }
  } catch {
    /* malformed JSON — treat as no default */
  }
  return '';
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
  const [costTouched, setCostTouched] = useState(false);
  const [note, setNote] = useState('');

  // Known inventory — feeds the brand datalist and the cost-per-kg prefill.
  // Only fetched while the modal is actually open; the app keeps this cache warm
  // elsewhere (Inventory page + WS invalidations), so it is usually instant.
  const { data: allSpools } = useQuery({
    queryKey: ['inventory-spools'],
    queryFn: () => api.getSpools(true),
    enabled: context != null,
    staleTime: 30_000,
  });

  // Distinct, non-archived brands, alphabetically — the zero-typing datalist.
  const brandOptions = Array.from(
    new Set(
      (allSpools ?? [])
        .filter(s => !s.archived_at)
        .map(s => s.brand?.trim())
        .filter((b): b is string => !!b),
    ),
  ).sort((a, b) => a.localeCompare(b));

  const material = context?.tray_type ?? '';

  // Most recent non-archived spool cost/kg for a brand: prefer the same material
  // as this tag, else any spool of that brand; blank when there is no prior.
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

  // Reset the form whenever a different slot's context arrives (queue advances
  // or a new manual open). `context` is a stable object per queue entry, so
  // this doesn't fire on unrelated parent re-renders.
  useEffect(() => {
    if (!context) return;
    const settings = queryClient.getQueryData<{ tagless_default_filament?: string | null }>([
      'settings',
    ]);
    const initialBrand =
      context.brand_prefill ||
      parseTaglessBrand(settings?.tagless_default_filament) ||
      readLastBrand();
    setBrand(initialBrand);
    setLabelWeight(context.label_weight_prefill != null ? String(context.label_weight_prefill) : '');
    setCostPerKg('');
    setCostTouched(false);
    setNote('');
  }, [context, queryClient]);

  // Seed / recompute cost from the chosen brand until the operator edits it
  // manually. Covers the initial open, a brand change (typed or picked), and the
  // spools query resolving after the modal opened.
  useEffect(() => {
    if (!context || costTouched) return;
    setCostPerKg(suggestCostForBrand(brand));
  }, [context, brand, costTouched, suggestCostForBrand]);

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

  const materialLabel = context.tray_sub_brands || context.tray_type || '—';
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
        {/* Material + colour swatch — the plain-language headline (no raw hex) */}
        <div className="flex items-center gap-3 p-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary">
          {swatchStyle && (
            <div
              className="w-8 h-8 rounded-full border border-black/20 flex-shrink-0"
              style={swatchStyle}
              aria-label={context.tray_color ?? undefined}
            />
          )}
          <div className="min-w-0 flex-1">
            <p className="text-white text-sm font-medium truncate">{materialLabel}</p>
          </div>
        </div>

        {/* Donor spool line — only when a Bambuddy row backs this tag. The
            internal record id lives in the title attribute, not the visible copy. */}
        {context.donor_spool_id != null && (
          <p
            className="text-xs text-bambu-gray"
            title={t('inventory.respool.donorRecordTitle', { id: context.donor_spool_id })}
          >
            {t('inventory.respool.donorLine', {
              grams: Math.max(0, Math.round(context.donor_remaining_g ?? 0)),
            })}
          </p>
        )}

        {/* Persistent one-tag-per-roll warning */}
        <div
          role="note"
          className="flex items-start gap-2 p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/40 text-yellow-700 dark:text-yellow-200 text-xs"
        >
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" aria-hidden="true" />
          <span>{t('inventory.respool.warning')}</span>
        </div>

        {/* Brand (required) — native datalist for zero-typing, free text allowed */}
        <div>
          <label htmlFor="respool-brand" className="block text-xs text-bambu-gray mb-1">
            {t('inventory.respool.brandLabel')}
          </label>
          <input
            id="respool-brand"
            type="text"
            list="respool-brand-options"
            value={brand}
            onChange={e => setBrand(e.target.value)}
            aria-required="true"
            placeholder={t('inventory.respool.brandPlaceholder')}
            className={inputClass}
          />
          <datalist id="respool-brand-options">
            {brandOptions.map(b => (
              <option key={b} value={b} />
            ))}
          </datalist>
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
              onChange={e => {
                setCostPerKg(e.target.value);
                setCostTouched(true);
              }}
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

        {/* Raw tag identity — de-jargoned out of the headline into a disclosure */}
        {tagIdentity && (
          <details className="text-xs text-bambu-gray">
            <summary className="cursor-pointer select-none hover:text-white">
              {t('inventory.respool.detailsLabel')}
            </summary>
            <div className="mt-2 flex items-center gap-2">
              <span>{t('inventory.respool.tagIdLabel')}</span>
              <span className="font-mono break-all text-bambu-gray">{tagIdentity}</span>
            </div>
          </details>
        )}
      </div>
    </ConfirmModal>
  );
}
