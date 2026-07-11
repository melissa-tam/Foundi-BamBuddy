/**
 * Production runs page (farm production, Phase 2).
 *
 * Starts runs (SKU → file/plate → target units → printer strategy → eject
 * profile + optional cooldown override) and renders live progress with
 * plates/units bars, status badges, humane ETA, and pause/resume/abort
 * controls (abort behind a required confirmation).
 *
 * The list polls every 5s via TanStack Query. Numeric form fields are held as
 * strings and coerced/validated once on submit. All copy is i18n; inputs are
 * label-linked (WCAG AA) and keyboard operable.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  ChevronDown,
  Factory,
  Loader2,
  Pause,
  Play,
  Plus,
  RotateCcw,
  Square,
  Trash2,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { FirstArticleBanner } from '../components/FirstArticleBanner';
import {
  BlockedPrintersChip,
  PauseReasonChip,
  RunStagedBanner,
  RunStatusBadge,
} from '../components/RunBadges';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatDuration } from '../utils/date';
import type { ProductionRun, ProductionRunCreate, RunPrefill } from '../types/productionRuns';

/** First-article run-policy bounds (mirror the backend validation). */
const RETRY_MIN = 0;
const RETRY_MAX = 5;
const ESCALATE_MIN = 1;
const ESCALATE_MAX = 10;
/** Fallbacks used only until the settings query resolves (or if it 403s for a
 *  user without settings:read — the policy still starts at sane defaults). */
const RETRY_FALLBACK = 1;
const ESCALATE_FALLBACK = 2; // must match backend farm_escalate_consecutive_failures default

/** Clamp a numeric string to [min, max], returning `fallback` when unparseable. */
function clampInt(raw: string, min: number, max: number, fallback: number): number {
  const n = Number(raw);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(n)));
}

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary ' +
  'focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

// ---------------------------------------------------------------------------
// Start-run dialog
// ---------------------------------------------------------------------------

interface StartRunDialogProps {
  saving: boolean;
  /** Backend failure detail from the last start attempt; rendered inline so a
   *  rejected run (e.g. 422 policy errors) never dead-ends silently. */
  error: string | null;
  /** Seed values from a finished run ("Run again", Phase 5, F9). When present the
   *  dialog seeds once from these instead of the first-runnable-SKU auto-seed. */
  initial?: RunPrefill;
  onStart: (data: ProductionRunCreate) => void;
  onClose: () => void;
}

function StartRunDialog({ saving, error, initial, onStart, onClose }: StartRunDialogProps) {
  const { t } = useTranslation();

  // All SKUs (including those with zero linked files), fetched here rather than
  // passed pre-filtered: a SKU the operator just created must still appear in
  // the dropdown — disabled — instead of silently vanishing. Shares the ['skus']
  // cache with the page; opening the dialog refetches so a just-created SKU
  // surfaces without a page reload.
  const { data: skusData } = useQuery({
    queryKey: ['skus'],
    queryFn: api.getSkus,
  });
  const skus = useMemo(() => skusData ?? [], [skusData]);
  // A SKU is runnable only once it links at least one file (a run needs
  // file + plate + units-per-plate). File-less SKUs render disabled.
  const runnableSkus = useMemo(() => skus.filter((s) => s.files.length > 0), [skus]);
  const hasUnrunnableSkus = useMemo(() => skus.some((s) => s.files.length === 0), [skus]);

  const [skuId, setSkuId] = useState<number | null>(null);
  const selectedSku = useMemo(() => skus.find((s) => s.id === skuId) ?? null, [skus, skuId]);
  const skuFiles = useMemo(() => selectedSku?.files ?? [], [selectedSku]);

  const [fileId, setFileId] = useState<number | null>(null);
  const [targetUnits, setTargetUnits] = useState<string>('1');
  const [mode, setMode] = useState<'specific' | 'model'>('model');
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [printerIds, setPrinterIds] = useState<number[]>([]);
  const [ejectProfileId, setEjectProfileId] = useState<number | null>(null);
  const [cooldownOverride, setCooldownOverride] = useState<string>('');
  const [requireFirstArticle, setRequireFirstArticle] = useState(true);
  const [retriesPerPlate, setRetriesPerPlate] = useState<string>(String(RETRY_FALLBACK));
  const [escalateFailures, setEscalateFailures] = useState<string>(String(ESCALATE_FALLBACK));
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [fileError, setFileError] = useState(false);
  const [targetError, setTargetError] = useState(false);
  const [printerError, setPrinterError] = useState(false);

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });
  // Distinct printer models across the fleet — the "any model" target options.
  // Derived (never hardcoded): an H2C fleet is targetable the moment its printers
  // are registered, with no code change (F5 removes the old hardcoded 'H2S').
  const models = useMemo(
    () => [...new Set((printers ?? []).map((p) => p.model).filter((m): m is string => !!m))].sort(),
    [printers],
  );
  const { data: ejectProfiles } = useQuery({
    queryKey: ['eject-profiles'],
    queryFn: api.getEjectProfiles,
  });
  // Prefill the retry / quarantine policy from the farm defaults. May 403 for a
  // user without settings:read — the query just errors and the fallback stands.
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  // One-shot seed guards: each ref lets a value seed once so a later query
  // refetch or operator edit is never clobbered. A Run-again prefill seeds first
  // and marks the fresh-dialog auto-seeds done so they don't overwrite it.
  const policyPrefilled = useRef(false);
  const skuSeeded = useRef(false);
  const modelTouched = useRef(false);
  const initialSeeded = useRef(false);

  // Run again (Phase 5, F9): seed the whole form from a finished run's prefill
  // exactly once. Resolve the owning SKU from the prefill's file id via the skus
  // query; if that file was since deleted the prefill can't resolve — fall back
  // to the fresh-dialog auto-seed (no dead end: the SKU picker + "no files"
  // notice still render). Declared BEFORE the auto-seed effects so, on the commit
  // where skus resolve, it can mark them done before they run.
  useEffect(() => {
    if (!initial || initialSeeded.current || skus.length === 0) return;
    const owner = skus.find((s) => s.files.some((f) => f.id === initial.skuFileId)) ?? null;
    initialSeeded.current = true;
    if (owner === null) return; // deleted file → let the fresh-dialog seed run
    policyPrefilled.current = true;
    skuSeeded.current = true;
    setSkuId(owner.id);
    setFileId(initial.skuFileId);
    setTargetUnits(String(initial.targetUnits));
    setMode(initial.mode);
    setPrinterIds(initial.printerIds);
    setEjectProfileId(initial.ejectProfileId);
    setCooldownOverride(initial.cooldownOverride != null ? String(initial.cooldownOverride) : '');
    setRequireFirstArticle(initial.requireFirstArticle);
    setRetriesPerPlate(String(initial.retryMaxPerUnit));
    setEscalateFailures(String(initial.escalateConsecutiveFailures));
    if (initial.targetModel) {
      setSelectedModel(initial.targetModel);
      modelTouched.current = true; // don't re-default off the file model
    }
  }, [initial, skus]);

  // Seed the two policy fields from settings exactly once, the first time they
  // resolve, so a later re-fetch never clobbers the operator's edits.
  useEffect(() => {
    if (policyPrefilled.current || !settings) return;
    policyPrefilled.current = true;
    if (typeof settings.farm_retry_max_per_unit === 'number') {
      setRetriesPerPlate(String(settings.farm_retry_max_per_unit));
    }
    if (typeof settings.farm_escalate_consecutive_failures === 'number') {
      setEscalateFailures(String(settings.farm_escalate_consecutive_failures));
    }
  }, [settings]);

  // Seed the SKU/file/eject defaults from the first *runnable* SKU once the list
  // resolves — a file-less SKU is never auto-selected (it can't run). Runs once
  // so it never clobbers a later operator selection (or a Run-again prefill).
  useEffect(() => {
    if (skuSeeded.current || runnableSkus.length === 0) return;
    skuSeeded.current = true;
    const first = runnableSkus[0];
    setSkuId(first.id);
    setFileId(first.files[0]?.id ?? null);
    setEjectProfileId(first.default_eject_profile_id ?? null);
  }, [runnableSkus]);

  const selectedFile = useMemo(() => skuFiles.find((f) => f.id === fileId) ?? null, [skuFiles, fileId]);
  const fileModel = selectedFile?.printer_model ?? null;

  // Default the target model to the selected file's slice model when the fleet
  // has it, else the first available model. Re-defaults when the file changes,
  // but only until the operator picks a model themselves (modelTouched) — and a
  // Run-again prefill that carried an explicit model already set modelTouched.
  useEffect(() => {
    if (modelTouched.current) return;
    setSelectedModel(fileModel && models.includes(fileModel) ? fileModel : (models[0] ?? null));
  }, [fileModel, models]);

  // The eject profile the cooldown override would supersede (Phase 4.3i).
  const selectedEjectProfile = useMemo(
    () => (ejectProfiles ?? []).find((p) => p.id === ejectProfileId) ?? null,
    [ejectProfiles, ejectProfileId],
  );

  // Farm capability gate owns enforcement server-side; the dialog only WARNS
  // when the file's slice model differs from where it's about to run — in model
  // mode against the chosen model, in specific mode against any checked printer.
  const modelMismatch = useMemo<string | null>(() => {
    if (!fileModel) return null;
    if (mode === 'model') {
      return selectedModel && selectedModel !== fileModel ? selectedModel : null;
    }
    const mismatched = (printers ?? []).find(
      (p) => printerIds.includes(p.id) && p.model && p.model !== fileModel,
    );
    return mismatched?.model ?? null;
  }, [fileModel, mode, selectedModel, printers, printerIds]);

  // Settings-seeded defaults for the advanced policy fields — used to decide
  // whether an advanced value is a non-default override worth surfacing.
  const retryDefault = settings?.farm_retry_max_per_unit ?? RETRY_FALLBACK;
  const escalateDefault = settings?.farm_escalate_consecutive_failures ?? ESCALATE_FALLBACK;

  // One-line summary of the non-default advanced values, shown next to the
  // collapsed "Advanced" label so an override is never invisible (F5). Composed
  // from the existing field labels (all i18n) — no new copy to translate.
  const advancedSummary = useMemo<string[]>(() => {
    const parts: string[] = [];
    if (ejectProfileId !== (selectedSku?.default_eject_profile_id ?? null)) {
      parts.push(
        `${t('productionRuns.fields.ejectProfile')}: ${
          selectedEjectProfile?.name ?? t('productionRuns.fields.noEjectProfile')
        }`,
      );
    }
    if (cooldownOverride.trim() !== '') {
      parts.push(`${t('productionRuns.fields.cooldownOverride')}: ${cooldownOverride.trim()}`);
    }
    if (clampInt(retriesPerPlate, RETRY_MIN, RETRY_MAX, RETRY_FALLBACK) !== retryDefault) {
      parts.push(`${t('productionRuns.firstArticle.retriesLabel')}: ${retriesPerPlate}`);
    }
    if (clampInt(escalateFailures, ESCALATE_MIN, ESCALATE_MAX, ESCALATE_FALLBACK) !== escalateDefault) {
      parts.push(`${t('productionRuns.firstArticle.escalateLabel')}: ${escalateFailures}`);
    }
    return parts;
  }, [
    t,
    ejectProfileId,
    selectedSku,
    selectedEjectProfile,
    cooldownOverride,
    retriesPerPlate,
    escalateFailures,
    retryDefault,
    escalateDefault,
  ]);

  // When the SKU changes, reset the dependent file + eject-profile defaults.
  const onSkuChange = (nextId: number | null) => {
    setSkuId(nextId);
    const next = skus.find((s) => s.id === nextId) ?? null;
    setFileId(next?.files[0]?.id ?? null);
    setEjectProfileId(next?.default_eject_profile_id ?? null);
    setFileError(false);
  };

  // Client-side over-production preview: whole plates that meet the target.
  const targetNum = Number(targetUnits);
  const overProduction = useMemo(() => {
    if (!selectedFile || !Number.isFinite(targetNum) || targetNum < 1) return null;
    const perPlate = selectedFile.units_per_plate;
    if (!perPlate || perPlate < 1) return null;
    const plates = Math.ceil(targetNum / perPlate);
    const planned = plates * perPlate;
    const extra = planned - targetNum;
    return extra > 0 ? { planned, plates, extra } : null;
  }, [selectedFile, targetNum]);

  const togglePrinter = (id: number) => {
    setPrinterError(false);
    setPrinterIds((prev) => (prev.includes(id) ? prev.filter((p) => p !== id) : [...prev, id]));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    let invalid = false;
    if (fileId === null) {
      setFileError(true);
      invalid = true;
    }
    if (!Number.isFinite(targetNum) || targetNum < 1) {
      setTargetError(true);
      invalid = true;
    }
    if (mode === 'specific' && printerIds.length === 0) {
      setPrinterError(true);
      invalid = true;
    }
    if (invalid) return;

    const cooldown = cooldownOverride.trim() === '' ? null : Number(cooldownOverride);
    const payload: ProductionRunCreate = {
      sku_file_id: fileId!,
      target_units: Math.floor(targetNum),
      eject_profile_id: ejectProfileId,
      cooldown_temp_c_override:
        cooldown != null && Number.isFinite(cooldown) ? cooldown : null,
      require_first_article: requireFirstArticle,
      retry_max_per_unit: clampInt(retriesPerPlate, RETRY_MIN, RETRY_MAX, RETRY_FALLBACK),
      escalate_consecutive_failures: clampInt(
        escalateFailures,
        ESCALATE_MIN,
        ESCALATE_MAX,
        ESCALATE_FALLBACK,
      ),
    };
    if (mode === 'specific') payload.printer_ids = printerIds;
    else if (selectedModel) payload.target_model = selectedModel;

    onStart(payload);
  };

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={saving ? undefined : onClose}
      role="dialog"
      aria-modal="true"
      aria-label={t('productionRuns.startTitle')}
    >
      <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <Factory className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">{t('productionRuns.startTitle')}</h2>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={saving} aria-label={t('common.close')}>
              <X className="w-5 h-5" />
            </Button>
          </div>

          {/* noValidate: the form owns validation so out-of-range values (e.g.
              target 0 with min=1) surface the i18n error text instead of the
              browser-native English bubble. */}
          <form onSubmit={handleSubmit} noValidate className="p-4 space-y-4">
            {/* SKU + file */}
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label htmlFor="run-sku" className="block text-sm font-medium text-white mb-1">
                  {t('productionRuns.fields.sku')}
                </label>
                <select
                  id="run-sku"
                  value={skuId ?? ''}
                  onChange={(e) => onSkuChange(e.target.value ? Number(e.target.value) : null)}
                  className={inputClass}
                >
                  {skus.map((s) => {
                    // File-less SKUs stay visible but disabled, so a SKU the
                    // operator just created is discoverable — and the reason it
                    // can't run yet is spelled out — instead of missing entirely.
                    const runnable = s.files.length > 0;
                    return (
                      <option key={s.id} value={s.id} disabled={!runnable}>
                        {runnable
                          ? `${s.code} — ${s.name}`
                          : `${s.code} — ${s.name} ${t('productionRuns.fields.skuNoFile')}`}
                      </option>
                    );
                  })}
                </select>
                {hasUnrunnableSkus && (
                  <p className="text-xs text-bambu-gray mt-1">
                    {t('productionRuns.fields.skuNoFileHint')}
                  </p>
                )}
              </div>
              <div>
                <label htmlFor="run-file" className="block text-sm font-medium text-white mb-1">
                  {t('productionRuns.fields.file')}
                </label>
                <select
                  id="run-file"
                  value={fileId ?? ''}
                  onChange={(e) => {
                    setFileId(e.target.value ? Number(e.target.value) : null);
                    setFileError(false);
                  }}
                  className={`${inputClass} ${fileError ? 'border-red-500' : ''}`}
                  aria-invalid={fileError}
                  aria-describedby={fileError ? 'run-file-error' : undefined}
                >
                  <option value="">{t('productionRuns.fields.filePlaceholder')}</option>
                  {skuFiles.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.library_file_name} · {t('productionRuns.fields.plateUnits', {
                        plate: f.plate_index,
                        units: f.units_per_plate,
                      })}
                    </option>
                  ))}
                </select>
                {fileError && (
                  <p id="run-file-error" className="text-red-400 text-xs mt-1">
                    {t('productionRuns.fileRequired')}
                  </p>
                )}
              </div>
            </div>

            {skuFiles.length === 0 && (
              <p className="text-sm text-yellow-300">{t('productionRuns.noFiles')}</p>
            )}

            {/* Target units */}
            <div>
              <label htmlFor="run-target" className="block text-sm font-medium text-white mb-1">
                {t('productionRuns.fields.targetUnits')}
              </label>
              <input
                id="run-target"
                type="number"
                inputMode="numeric"
                min={1}
                max={100000}
                step={1}
                value={targetUnits}
                onChange={(e) => {
                  setTargetUnits(e.target.value);
                  if (targetError) setTargetError(false);
                }}
                className={`${inputClass} ${targetError ? 'border-red-500' : ''}`}
                aria-invalid={targetError}
                aria-describedby={targetError ? 'run-target-error' : undefined}
              />
              {targetError && (
                <p id="run-target-error" className="text-red-400 text-xs mt-1">
                  {t('productionRuns.targetRequired')}
                </p>
              )}
              {overProduction && (
                <p className="text-xs text-yellow-300 mt-1">
                  {t('productionRuns.overProduction', {
                    planned: overProduction.planned,
                    plates: overProduction.plates,
                    extra: overProduction.extra,
                    target: Math.floor(targetNum),
                  })}
                </p>
              )}
            </div>

            {/* Printer strategy */}
            <fieldset>
              <legend className="text-sm font-medium text-white mb-2">
                {t('productionRuns.fields.printers')}
              </legend>
              <div className="space-y-2">
                {/* "Any printer of model X" — the model list is derived from the
                    fleet (F5), so an H2C fleet is targetable with no code change.
                    >1 model → a picker; exactly 1 → static text; 0 printers →
                    the option is disabled (nothing to target). */}
                <div className="flex flex-wrap items-center gap-2">
                  <label htmlFor="run-mode-model" className="flex items-center gap-2 text-sm text-white cursor-pointer">
                    <input
                      id="run-mode-model"
                      type="radio"
                      name="run-printer-mode"
                      checked={mode === 'model'}
                      onChange={() => {
                        setMode('model');
                        setPrinterError(false);
                      }}
                      disabled={models.length === 0}
                      className="accent-bambu-green"
                    />
                    {models.length > 1
                      ? t('productionRuns.fields.modelSelect')
                      : models.length === 1
                        ? t('productionRuns.fields.anyModel', { model: models[0] })
                        : t('common.noPrinters')}
                  </label>
                  {models.length > 1 && (
                    <select
                      aria-label={t('productionRuns.fields.modelSelect')}
                      value={selectedModel ?? ''}
                      onChange={(e) => {
                        setSelectedModel(e.target.value);
                        modelTouched.current = true;
                        setMode('model');
                        setPrinterError(false);
                      }}
                      className="px-2 py-1 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary text-sm focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors"
                    >
                      {models.map((m) => (
                        <option key={m} value={m}>
                          {t('productionRuns.fields.anyModel', { model: m })}
                        </option>
                      ))}
                    </select>
                  )}
                </div>
                <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
                  <input
                    type="radio"
                    name="run-printer-mode"
                    checked={mode === 'specific'}
                    onChange={() => setMode('specific')}
                    className="accent-bambu-green"
                  />
                  {t('productionRuns.fields.specificPrinters')}
                </label>
              </div>

              {mode === 'specific' && (
                <div className="mt-2 pl-6 space-y-1">
                  {(printers ?? []).length === 0 ? (
                    <p className="text-sm text-bambu-gray italic">{t('common.noPrinters')}</p>
                  ) : (
                    (printers ?? []).map((p) => (
                      <label key={p.id} className="flex items-center gap-2 text-sm text-bambu-gray cursor-pointer">
                        <input
                          type="checkbox"
                          checked={printerIds.includes(p.id)}
                          onChange={() => togglePrinter(p.id)}
                          className="accent-bambu-green"
                        />
                        <span className="text-white">{p.name}</span>
                        {p.model ? <span className="text-xs text-bambu-gray">({p.model})</span> : null}
                      </label>
                    ))
                  )}
                  {printerError && (
                    <p className="text-red-400 text-xs mt-1">{t('productionRuns.printerRequired')}</p>
                  )}
                </div>
              )}

              {/* Soft slice-model mismatch note (never a block — the server-side
                  capability gate holds mismatched printers). Warns in either
                  strategy when the file's slice model differs from the target. */}
              {modelMismatch && (
                <p className="text-xs text-yellow-300 mt-2">
                  {t('productionRuns.fields.modelMismatchWarning', {
                    fileModel,
                    model: modelMismatch,
                  })}
                </p>
              )}
            </fieldset>

            {/* First-article approval (Phase 3) — stays in the primary flow;
                it changes what the run DOES, unlike the rarely-touched policy
                overrides now grouped under Advanced (F5). */}
            <fieldset className="rounded-lg border border-bambu-dark-tertiary p-3">
              <legend className="px-1 text-sm font-medium text-white">
                {t('productionRuns.firstArticle.sectionTitle')}
              </legend>
              <label className="flex items-start gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={requireFirstArticle}
                  onChange={(e) => setRequireFirstArticle(e.target.checked)}
                  className="mt-0.5 accent-bambu-green"
                />
                <span>
                  <span className="block text-sm text-white">
                    {t('productionRuns.firstArticle.requireLabel')}
                  </span>
                  <span className="block text-xs text-bambu-gray mt-0.5">
                    {t('productionRuns.firstArticle.requireHelp')}
                  </span>
                </span>
              </label>
            </fieldset>

            {/* Advanced overrides — collapsed by default (eject/cooldown/retry
                policy is rarely changed). When collapsed, any non-default value
                is surfaced in a one-line summary so an override is never hidden. */}
            <div>
              <button
                type="button"
                onClick={() => setAdvancedOpen((o) => !o)}
                aria-expanded={advancedOpen}
                aria-controls="run-advanced-section"
                className="w-full flex items-center justify-between gap-2 text-left rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/60"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <span className="text-sm font-medium text-white">{t('productionRuns.advanced')}</span>
                  {!advancedOpen && advancedSummary.length > 0 && (
                    <span className="text-xs text-bambu-gray truncate">{advancedSummary.join(' · ')}</span>
                  )}
                </span>
                <ChevronDown
                  className={`w-4 h-4 text-bambu-gray flex-shrink-0 transition-transform ${advancedOpen ? 'rotate-180' : ''}`}
                />
              </button>

              {advancedOpen && (
                <div id="run-advanced-section" className="mt-3 space-y-4">
                  {/* Eject profile override + cooldown override */}
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <label htmlFor="run-eject" className="block text-sm text-bambu-gray mb-1">
                        {t('productionRuns.fields.ejectProfile')}
                      </label>
                      <select
                        id="run-eject"
                        value={ejectProfileId ?? ''}
                        onChange={(e) => setEjectProfileId(e.target.value ? Number(e.target.value) : null)}
                        className={inputClass}
                      >
                        <option value="">{t('productionRuns.fields.noEjectProfile')}</option>
                        {(ejectProfiles ?? []).map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name}
                            {selectedSku?.default_eject_profile_id === p.id
                              ? ` (${t('productionRuns.fields.skuDefault')})`
                              : ''}
                          </option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label htmlFor="run-cooldown" className="block text-sm text-bambu-gray mb-1">
                        {t('productionRuns.fields.cooldownOverride')}
                      </label>
                      <input
                        id="run-cooldown"
                        type="number"
                        inputMode="decimal"
                        min={15}
                        max={60}
                        step={0.5}
                        placeholder={t('productionRuns.fields.cooldownPlaceholder')}
                        value={cooldownOverride}
                        onChange={(e) => setCooldownOverride(e.target.value)}
                        className={inputClass}
                        aria-describedby={selectedEjectProfile ? 'run-cooldown-default' : undefined}
                      />
                      {/* Phase 4.3i: show what an override would replace, from the
                          already-fetched profiles list. */}
                      {selectedEjectProfile && (
                        <p id="run-cooldown-default" className="text-xs text-bambu-gray mt-1">
                          {t('productionRuns.fields.cooldownProfileDefault', {
                            value: selectedEjectProfile.cooldown_temp_c,
                          })}
                        </p>
                      )}
                    </div>
                  </div>

                  {/* Retry / quarantine policy */}
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <label htmlFor="run-retries" className="block text-sm text-bambu-gray mb-1">
                        {t('productionRuns.firstArticle.retriesLabel')}
                      </label>
                      <input
                        id="run-retries"
                        type="number"
                        inputMode="numeric"
                        min={RETRY_MIN}
                        max={RETRY_MAX}
                        step={1}
                        value={retriesPerPlate}
                        onChange={(e) => setRetriesPerPlate(e.target.value)}
                        className={inputClass}
                      />
                    </div>
                    <div>
                      <label htmlFor="run-escalate" className="block text-sm text-bambu-gray mb-1">
                        {t('productionRuns.firstArticle.escalateLabel')}
                      </label>
                      <input
                        id="run-escalate"
                        type="number"
                        inputMode="numeric"
                        min={ESCALATE_MIN}
                        max={ESCALATE_MAX}
                        step={1}
                        value={escalateFailures}
                        onChange={(e) => setEscalateFailures(e.target.value)}
                        className={inputClass}
                      />
                    </div>
                  </div>
                </div>
              )}
            </div>

            {/* Backend rejection (persistent, unlike a toast — the dialog is
                the surface the user is looking at when the POST fails). */}
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
              <Button type="submit" className="flex-1" disabled={saving || skuFiles.length === 0}>
                {saving ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    {t('productionRuns.starting')}
                  </>
                ) : (
                  <>
                    <Play className="w-4 h-4" />
                    {t('productionRuns.startAction')}
                  </>
                )}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Run card
// ---------------------------------------------------------------------------

function RunCard({
  run,
  onPause,
  onResume,
  onAbort,
  onDelete,
  onRunAgain,
  canDelete,
  canRunAgain,
  mutatingId,
}: {
  run: ProductionRun;
  onPause: (id: number) => void;
  onResume: (id: number) => void;
  onAbort: (run: ProductionRun) => void;
  onDelete: (run: ProductionRun) => void;
  onRunAgain: (run: ProductionRun) => void;
  canDelete: boolean;
  canRunAgain: boolean;
  mutatingId: number | null;
}) {
  const { t } = useTranslation();
  const busy = mutatingId === run.id;
  const platePct =
    run.plates_total > 0 ? Math.min(100, Math.round((run.plates_completed / run.plates_total) * 100)) : 0;
  const isTerminal = run.status === 'completed' || run.status === 'cancelled';

  return (
    <Card>
      <CardContent>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              {/* Title links to the per-unit detail page (Phase 4.1); the card
                  itself stays a plain container so the action buttons keep
                  their own click targets. */}
              <h3 className="text-base font-semibold truncate">
                <Link
                  to={`/production-runs/${run.id}`}
                  className="text-white hover:text-bambu-green transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/60 rounded"
                  title={t('productionRuns.viewDetails')}
                >
                  {run.name}
                </Link>
              </h3>
              <RunStatusBadge status={run.status} />
              <PauseReasonChip run={run} />
              <BlockedPrintersChip run={run} />
            </div>
            <p className="text-sm text-bambu-gray mt-0.5">
              {run.sku_code}
              {run.printers.length > 0 && (
                <span> · {run.printers.map((p) => p.name).join(', ')}</span>
              )}
            </p>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {run.status === 'active' && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => onPause(run.id)}
                disabled={busy}
                aria-label={t('productionRuns.pause')}
              >
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Pause className="w-4 h-4" />}
                {t('productionRuns.pause')}
              </Button>
            )}
            {run.status === 'paused' && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => onResume(run.id)}
                disabled={busy}
                aria-label={t('productionRuns.resume')}
              >
                {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                {t('productionRuns.resume')}
              </Button>
            )}
            {!isTerminal && (
              <Button
                variant="danger"
                size="sm"
                onClick={() => onAbort(run)}
                disabled={busy}
                aria-label={t('productionRuns.abort')}
              >
                <Square className="w-4 h-4" />
                {t('productionRuns.abort')}
              </Button>
            )}
            {/* Run again re-opens the start dialog prefilled from this finished
                run (F9); offered on terminal runs to operators who can create. */}
            {isTerminal && canRunAgain && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => onRunAgain(run)}
                disabled={busy}
                aria-label={t('productionRuns.runAgain')}
              >
                <RotateCcw className="w-4 h-4" />
                {t('productionRuns.runAgain')}
              </Button>
            )}
            {/* Delete is offered only once a run is finished (cancelled /
                completed) and only to operators holding production_runs:delete. */}
            {isTerminal && canDelete && (
              <Button
                variant="danger"
                size="sm"
                onClick={() => onDelete(run)}
                disabled={busy}
                aria-label={t('productionRuns.delete')}
              >
                <Trash2 className="w-4 h-4" />
                {t('productionRuns.delete')}
              </Button>
            )}
          </div>
        </div>

        {/* Plate progress bar */}
        <div className="mt-4">
          <div className="flex items-center justify-between text-xs text-bambu-gray mb-1">
            <span>
              {t('productionRuns.platesProgress', {
                completed: run.plates_completed,
                total: run.plates_total,
              })}
            </span>
            <span>{platePct}%</span>
          </div>
          <div
            className="h-2 bg-bambu-dark rounded-full overflow-hidden"
            role="progressbar"
            aria-valuenow={platePct}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-label={t('productionRuns.platesProgress', {
              completed: run.plates_completed,
              total: run.plates_total,
            })}
          >
            <div className="h-full bg-bambu-green transition-all" style={{ width: `${platePct}%` }} />
          </div>
        </div>

        {/* Stat grid */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 text-sm">
          <div>
            <p className="text-bambu-gray text-xs">{t('productionRuns.stats.units')}</p>
            <p className="text-white tabular-nums">
              {run.units_completed} / {run.units_planned}
            </p>
          </div>
          <div>
            <p className="text-bambu-gray text-xs">{t('productionRuns.stats.target')}</p>
            <p className="text-white tabular-nums">{run.target_units}</p>
          </div>
          <div>
            <p className="text-bambu-gray text-xs">{t('productionRuns.stats.failed')}</p>
            <p className="text-white tabular-nums">
              {run.units_failed} · {run.plates_failed}
            </p>
          </div>
          <div>
            <p className="text-bambu-gray text-xs">{t('productionRuns.stats.eta')}</p>
            <p className="text-white tabular-nums">
              {run.status === 'active' && run.eta_seconds != null
                ? formatDuration(run.eta_seconds)
                : '—'}
            </p>
          </div>
        </div>

        {/* Staged units (low-spool / other holds, Phase 4.1) */}
        <RunStagedBanner run={run} />

        {/* First-article approval gate (Phase 3) */}
        <FirstArticleBanner run={run} />
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function ProductionRunsPage() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();

  const canDelete = hasPermission('production_runs:delete');
  const canCreate = hasPermission('production_runs:create');

  const [dialogOpen, setDialogOpen] = useState(false);
  // Seed values for a "Run again" (F9); null for a fresh start.
  const [prefill, setPrefill] = useState<RunPrefill | null>(null);
  const [pendingAbort, setPendingAbort] = useState<ProductionRun | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ProductionRun | null>(null);

  const {
    data: runs,
    isLoading,
    isError,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ['production-runs'],
    queryFn: api.getProductionRuns,
    refetchInterval: 5000,
  });

  // SKUs feed the start-run dialog's SKU/file selects.
  const { data: skus } = useQuery({
    queryKey: ['skus'],
    queryFn: api.getSkus,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['production-runs'] });

  const startMutation = useMutation({
    mutationFn: (data: ProductionRunCreate) => api.createProductionRun(data),
    onSuccess: () => {
      showToast(t('productionRuns.started'));
      invalidate();
      setDialogOpen(false);
      setPrefill(null);
    },
    // No error toast: the failure detail renders inline inside the open
    // dialog (StartRunDialog `error` prop) so it cannot be missed/dismissed.
  });

  const openStartDialog = () => {
    // Clear any error left over from a previous failed attempt so the dialog
    // opens clean; a fresh start carries no prefill.
    startMutation.reset();
    setPrefill(null);
    setDialogOpen(true);
  };

  // Run again (F9): reopen the dialog seeded from a finished run. Model-targeted
  // runs carry target_model (no printers); specific-printer runs carry printers.
  const openRunAgain = (run: ProductionRun) => {
    startMutation.reset();
    const hasModel = run.target_model != null && run.target_model !== '';
    setPrefill({
      skuFileId: run.sku_file_id,
      targetUnits: run.target_units,
      mode: hasModel ? 'model' : 'specific',
      printerIds: hasModel ? [] : run.printers.map((p) => p.id),
      targetModel: run.target_model ?? null,
      ejectProfileId: run.eject_profile_id,
      cooldownOverride: run.cooldown_temp_c_override,
      requireFirstArticle: run.require_first_article,
      retryMaxPerUnit: run.retry_max_per_unit,
      escalateConsecutiveFailures: run.escalate_consecutive_failures,
    });
    setDialogOpen(true);
  };

  const closeStartDialog = () => {
    setDialogOpen(false);
    setPrefill(null);
  };

  const pauseMutation = useMutation({
    mutationFn: (id: number) => api.pauseProductionRun(id),
    onSuccess: () => {
      showToast(t('productionRuns.paused'));
      invalidate();
    },
    onError: (err: Error) => showToast(err.message || t('productionRuns.actionFailed'), 'error'),
  });

  const resumeMutation = useMutation({
    mutationFn: (id: number) => api.resumeProductionRun(id),
    onSuccess: () => {
      showToast(t('productionRuns.resumed'));
      invalidate();
    },
    onError: (err: Error) => showToast(err.message || t('productionRuns.actionFailed'), 'error'),
  });

  const abortMutation = useMutation({
    mutationFn: (id: number) => api.abortProductionRun(id),
    onSuccess: () => {
      showToast(t('productionRuns.aborted'));
      invalidate();
      setPendingAbort(null);
    },
    onError: (err: Error) => {
      showToast(err.message || t('productionRuns.actionFailed'), 'error');
      setPendingAbort(null);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteProductionRun(id),
    onSuccess: () => {
      showToast(t('productionRuns.deleted'));
      invalidate();
      setPendingDelete(null);
    },
    // Deliberately no onError close: a failure (e.g. a 409 if the run left the
    // terminal state) stays inline in the still-open confirm dialog as a
    // persistent role="alert" rather than a dismissible toast.
  });

  const mutatingId =
    pauseMutation.isPending
      ? (pauseMutation.variables as number)
      : resumeMutation.isPending
        ? (resumeMutation.variables as number)
        : abortMutation.isPending
          ? (abortMutation.variables as number)
          : null;

  const list = runs ?? [];
  const skuList = skus ?? [];
  const canStart = skuList.some((s) => s.files.length > 0);

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <Factory className="w-6 h-6 text-bambu-green" />
            {t('productionRuns.title')}
          </h1>
          <p className="text-sm text-bambu-gray mt-1 max-w-2xl">{t('productionRuns.description')}</p>
        </div>
        {list.length > 0 && (
          <Button onClick={openStartDialog} disabled={!canStart}>
            <Plus className="w-4 h-4" />
            {t('productionRuns.startAction')}
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
            <p className="text-white mb-4">{t('productionRuns.loadError')}</p>
            <Button variant="secondary" onClick={() => refetch()} disabled={isFetching}>
              {isFetching ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              {t('common.retry')}
            </Button>
          </CardContent>
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center text-center py-16">
            <Factory className="w-12 h-12 text-bambu-gray mb-4" />
            <h2 className="text-lg font-semibold text-white mb-1">{t('productionRuns.empty.title')}</h2>
            <p className="text-sm text-bambu-gray mb-5 max-w-md">
              {canStart ? t('productionRuns.empty.body') : t('productionRuns.empty.noSkus')}
            </p>
            <Button onClick={openStartDialog} disabled={!canStart}>
              <Plus className="w-4 h-4" />
              {t('productionRuns.startAction')}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {list.map((run) => (
            <RunCard
              key={run.id}
              run={run}
              onPause={(id) => pauseMutation.mutate(id)}
              onResume={(id) => resumeMutation.mutate(id)}
              onAbort={(r) => setPendingAbort(r)}
              onDelete={(r) => {
                // Clear any error from a previous attempt so the dialog opens clean.
                deleteMutation.reset();
                setPendingDelete(r);
              }}
              onRunAgain={openRunAgain}
              canDelete={canDelete}
              canRunAgain={canCreate}
              mutatingId={mutatingId}
            />
          ))}
        </div>
      )}

      {dialogOpen && (
        <StartRunDialog
          saving={startMutation.isPending}
          error={
            startMutation.error
              ? startMutation.error.message || t('productionRuns.startFailed')
              : null
          }
          initial={prefill ?? undefined}
          onStart={(data) => startMutation.mutate(data)}
          onClose={closeStartDialog}
        />
      )}

      {pendingAbort && (
        <ConfirmModal
          title={t('productionRuns.abortTitle')}
          message={t('productionRuns.abortBody', { name: pendingAbort.name })}
          confirmText={t('productionRuns.abort')}
          cancelText={t('common.cancel')}
          variant="danger"
          isLoading={abortMutation.isPending}
          onConfirm={() => abortMutation.mutate(pendingAbort.id)}
          onCancel={() => setPendingAbort(null)}
        />
      )}

      {pendingDelete && (
        <ConfirmModal
          title={t('productionRuns.deleteTitle')}
          message={t('productionRuns.deleteBody', { name: pendingDelete.name })}
          confirmText={t('productionRuns.delete')}
          cancelText={t('common.cancel')}
          variant="danger"
          isLoading={deleteMutation.isPending}
          onConfirm={() => deleteMutation.mutate(pendingDelete.id)}
          onCancel={() => setPendingDelete(null)}
        >
          {/* Persistent inline failure (same house pattern as the reject
              dialog above) — the confirm dialog is the surface in focus when
              the DELETE fails, so the detail must not vanish as a toast. */}
          {deleteMutation.error && (
            <div
              role="alert"
              className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300"
            >
              <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
              <span>{deleteMutation.error.message || t('productionRuns.deleteFailed')}</span>
            </div>
          )}
        </ConfirmModal>
      )}
    </div>
  );
}

export default ProductionRunsPage;
