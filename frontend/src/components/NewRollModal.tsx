import { useCallback, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import { ConfirmModal } from './ConfirmModal';
import { InlineAlert } from './ui/InlineAlert';
import { api } from '../api/client';
import type { InventorySpool, Printer } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { getAmsLabel } from '../utils/amsHelpers';
import { getSwatchStyle } from '../utils/colors';
import type { NewRollContext } from '../utils/newRollContext';

/**
 * "New roll…" — the ONE form behind the ONE slot verb for "the roll on this slot
 * was physically replaced".
 *
 * It replaced two forms that asked the same question of the operator and differed
 * only in the BOOKKEEPING behind it. Tag-ness is not something an operator should
 * have to classify, so the caller states it (`tagged`, read off the bound row) and
 * the single endpoint `POST /inventory/spools/{id}/new-roll` picks the ledger lane:
 * a tagless row is archived and re-minted, a tagged row's Bambu tag is re-spooled
 * onto a fresh full third-party record.
 *
 * Doctrine (rule 10): this form does NOT decide that a tag was reused. An operator
 * asserting a new roll on a tagged core is an operator STATEMENT, and the backend
 * closes the loop through `slot_recheck.note_operator_statement`.
 *
 * Four entry points, one component:
 *  - the per-slot verb on the printer card and the SpoolBuddy slot picker
 *    (`origin: 'manual'` — the operator's own assertion, no question was raised),
 *  - the tagless fresh-roll prompt's "Review…" action,
 *  - the reused-tag re-spool prompt's "Review…" action (an ESCALATION: the farm
 *    knows what happened and cannot carry it out on its own).
 * The quick "same roll / same spool" answers stay on their toasts — this form only
 * ever records a replacement.
 */

// Client-side fallback for the brand prefill when the backend didn't supply one
// (the manual slot verb, or the very first re-spool before a brand is set).
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

interface NewRollModalProps {
  /** Slot + prefill context; `null` keeps the modal closed. */
  context: NewRollContext | null;
  /** Called after a successful record AND when the operator cancels. */
  onClose: () => void;
}

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green';

export function NewRollModal({ context, onClose }: NewRollModalProps) {
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

  const material = context?.material ?? '';

  // Most recent non-archived spool cost/kg for a brand: prefer the same material
  // as this slot, else any spool of that brand; blank when there is no prior.
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

  const newRollMutation = useMutation({
    mutationFn: () => {
      if (!context) throw new Error('no context');
      const weightNum = labelWeight.trim() === '' ? null : Number(labelWeight);
      const costNum = costPerKg.trim() === '' ? null : Number(costPerKg);
      return api.newRoll(context.spool_id, {
        printer_id: context.printer_id,
        ams_id: context.ams_id,
        tray_id: context.tray_id,
        brand: brand.trim() === '' ? null : brand.trim(),
        label_weight: weightNum != null && Number.isFinite(weightNum) ? weightNum : null,
        cost_per_kg: costNum != null && Number.isFinite(costNum) ? costNum : null,
        note: note.trim() === '' ? null : note.trim(),
      });
    },
    onSuccess: (spool) => {
      writeLastBrand(brand.trim());
      showToast(
        context?.tagged
          ? t('inventory.respool.success', { brand: spool.brand ?? brand.trim() })
          : t('inventory.freshRoll.success'),
        'success',
      );
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      if (context) {
        queryClient.invalidateQueries({ queryKey: ['printerStatus', context.printer_id] });
      }
      onClose();
    },
    // No error toast: the dialog is the surface in focus, so the backend's
    // user-actionable detail (409 unbound row / sibling-tag conflict / Spoolman
    // mode, 400 empty slot or no tag, 404 printer offline) is rendered inline
    // below — the repo's documented failure-surfacing convention
    // (`ToastContext.TOAST_DURATION_MS`).
  });

  const { reset: resetNewRollMutation } = newRollMutation;

  // Reset the form whenever a different slot's context arrives (queue advances or
  // a new manual open) — including the mutation, so a previous slot's failure
  // never greets the next open. `context` is a stable object per entry, so this
  // doesn't fire on unrelated parent re-renders.
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
    resetNewRollMutation();
  }, [context, queryClient, resetNewRollMutation]);

  // Seed / recompute cost from the chosen brand until the operator edits it
  // manually. Covers the initial open, a brand change (typed or picked), and the
  // spools query resolving after the modal opened.
  useEffect(() => {
    if (!context || costTouched) return;
    setCostPerKg(suggestCostForBrand(brand));
  }, [context, brand, costTouched, suggestCostForBrand]);

  if (!context) return null;

  const tagged = context.tagged;
  const manual = context.origin === 'manual';
  const trayCount = context.tray_count ?? 4;
  const printers = queryClient.getQueryData<Printer[]>(['printers']);
  const printerName = printers?.find(p => p.id === context.printer_id)?.name ?? `Printer ${context.printer_id}`;
  const amsLabel = getAmsLabel(context.ams_id, trayCount);
  const slotLabel = `${t('inventory.unknownSpoolSlot', 'Slot')} ${context.tray_id + 1}`;
  const location = `${printerName} • ${amsLabel} • ${slotLabel}`;

  const materialLabel = context.material || '—';
  const swatchStyle = context.rgba ? getSwatchStyle(context.rgba) : undefined;
  const usedGrams = Math.max(0, Math.round(context.used_g ?? 0));
  const remainingGrams =
    context.remaining_g != null ? Math.max(0, Math.round(context.remaining_g)) : null;

  // A tagless mint falls back to the configured default filament, so only the
  // reused-tag lane needs a brand (the backend enforces the same rule, 422).
  const brandValid = !tagged || brand.trim().length > 0;

  // Copy namespace follows the LEDGER OPERATION, not the verb: a reused Bambu tag
  // moving onto a fresh roll and a tagless record being retired are different
  // facts and the dialog must state the one that is happening. The verb above it
  // is single ("New roll…") because the operator's ACT is single.
  const ns = tagged ? 'inventory.respool' : 'inventory.freshRoll';

  // Header framing follows the prompt's trigger (see `RespoolTrigger`). A
  // `near_empty` prompt only means "this roll is nearly used up and the slot was
  // handled" — claiming a reused tag had been detected there was the misleading
  // copy behind the 2026-07-20 false popups. `spent` / `remain_jump` keep the
  // reused-tag framing and add the specific evidence line underneath.
  const nearEmpty = tagged && context.trigger === 'near_empty';
  const titleKey = manual
    ? `${ns}.manualTitle`
    : nearEmpty
      ? `${ns}.nearEmptyTitle`
      : `${ns}.title`;
  const messageKey = manual
    ? `${ns}.manualMessage`
    : nearEmpty
      ? `${ns}.nearEmptyMessage`
      : `${ns}.message`;
  const confirmKey = tagged
    ? 'inventory.respool.confirm'
    : manual
      ? 'inventory.freshRoll.manualConfirm'
      : 'inventory.freshRoll.confirm';

  const triggerLineKey =
    context.trigger === 'spent'
      ? 'inventory.respool.triggerSpent'
      : context.trigger === 'remain_jump'
        ? 'inventory.respool.triggerRemainJump'
        : null;

  return (
    <ConfirmModal
      title={t(titleKey)}
      message={t(messageKey, { location, grams: usedGrams })}
      confirmText={t(confirmKey)}
      cancelText={t('common.cancel')}
      variant="default"
      isLoading={newRollMutation.isPending}
      loadingText={t(`${ns}.pending`)}
      confirmDisabled={!brandValid}
      onConfirm={() => {
        if (brandValid && !newRollMutation.isPending) newRollMutation.mutate();
      }}
      onCancel={onClose}
    >
      <div className="space-y-3">
        {/* Backend refusal stays on screen next to the button that triggered it. */}
        {newRollMutation.isError && (
          <InlineAlert severity="error">
            {newRollMutation.error instanceof Error && newRollMutation.error.message
              ? newRollMutation.error.message
              : t(`${ns}.failed`)}
          </InlineAlert>
        )}

        {/* Why this was raised — the specific evidence behind the reused-tag framing */}
        {triggerLineKey && <p className="text-xs text-bambu-gray">{t(triggerLineKey)}</p>}

        {/* Material + colour swatch — the plain-language headline (no raw hex) */}
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
            {!tagged && remainingGrams != null && (
              <p className="text-xs text-bambu-gray">
                {t('inventory.freshRoll.remaining', { grams: remainingGrams })}
              </p>
            )}
          </div>
        </div>

        {/* Retiring record line — the reused-tag lane replaces a TRACKED donor row,
            so it names what is being archived. The internal record id lives in the
            title attribute, not the visible copy. */}
        {tagged && remainingGrams != null && (
          <p
            className="text-xs text-bambu-gray"
            title={t('inventory.respool.donorRecordTitle', { id: context.spool_id })}
          >
            {t('inventory.respool.donorLine', { grams: remainingGrams })}
          </p>
        )}

        {/* Persistent one-tag-per-roll warning — reused tags only */}
        {tagged && (
          <div
            role="note"
            className="flex items-start gap-2 p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/40 text-yellow-700 dark:text-yellow-200 text-xs"
          >
            <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" aria-hidden="true" />
            <span>{t('inventory.respool.warning')}</span>
          </div>
        )}

        {/* Brand — required on the reused-tag lane, optional on the tagless one */}
        <div>
          <label htmlFor="new-roll-brand" className="block text-xs text-bambu-gray mb-1">
            {t(`${ns}.brandLabel`)}
          </label>
          <input
            id="new-roll-brand"
            type="text"
            list="new-roll-brand-options"
            value={brand}
            onChange={e => setBrand(e.target.value)}
            aria-required={tagged ? 'true' : undefined}
            placeholder={t(`${ns}.brandPlaceholder`)}
            className={inputClass}
          />
          <datalist id="new-roll-brand-options">
            {brandOptions.map(b => (
              <option key={b} value={b} />
            ))}
          </datalist>
        </div>

        {/* Label weight + cost/kg */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label htmlFor="new-roll-weight" className="block text-xs text-bambu-gray mb-1">
              {t(`${ns}.weightLabel`)}
            </label>
            <input
              id="new-roll-weight"
              type="number"
              min={0}
              value={labelWeight}
              onChange={e => setLabelWeight(e.target.value)}
              placeholder={t(`${ns}.weightPlaceholder`)}
              className={inputClass}
            />
          </div>
          <div className="flex-1">
            <label htmlFor="new-roll-cost" className="block text-xs text-bambu-gray mb-1">
              {t(`${ns}.costLabel`)}
            </label>
            <input
              id="new-roll-cost"
              type="number"
              min={0}
              step={0.01}
              value={costPerKg}
              onChange={e => {
                setCostPerKg(e.target.value);
                setCostTouched(true);
              }}
              placeholder={t(`${ns}.costPlaceholder`)}
              className={inputClass}
            />
          </div>
        </div>

        {/* Optional note */}
        <div>
          <label htmlFor="new-roll-note" className="block text-xs text-bambu-gray mb-1">
            {t(`${ns}.noteLabel`)}
          </label>
          <input
            id="new-roll-note"
            type="text"
            value={note}
            onChange={e => setNote(e.target.value)}
            placeholder={t(`${ns}.notePlaceholder`)}
            className={inputClass}
          />
        </div>

        {/* Raw tag identity — de-jargoned out of the headline into a disclosure */}
        {tagged && context.tag_identity && (
          <details className="text-xs text-bambu-gray">
            <summary className="cursor-pointer select-none hover:text-white">
              {t('inventory.respool.detailsLabel')}
            </summary>
            <div className="mt-2 flex items-center gap-2">
              <span>{t('inventory.respool.tagIdLabel')}</span>
              <span className="font-mono break-all text-bambu-gray">{context.tag_identity}</span>
            </div>
          </details>
        )}
      </div>
    </ConfirmModal>
  );
}
