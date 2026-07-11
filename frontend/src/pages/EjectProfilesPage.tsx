/**
 * Eject Profiles page (farm auto part-removal, Phase 1).
 *
 * Full CRUD over eject profiles plus a preview/validate panel that generates
 * the eject G-code for a chosen library file + plate through the backend and
 * surfaces the validation result (errors in red, warnings in amber) with a
 * collapsible monospace G-code viewer.
 *
 * Server state is TanStack Query; the numeric form keeps its values as strings
 * so decimals type cleanly and are coerced/validated once on submit. All copy
 * is i18n; inputs are label-linked (WCAG AA) and keyboard operable.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertCircle,
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Download,
  FlaskConical,
  Info,
  Loader2,
  PackageOpen,
  Pencil,
  Plus,
  Send,
  Trash2,
  X,
} from 'lucide-react';
import { api } from '../api/client';
import { usePlateIndexSync } from '../hooks/usePlateIndexSync';
import { Card, CardContent } from '../components/Card';
import { Button } from '../components/Button';
import { ConfirmModal } from '../components/ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import {
  DEFAULT_EJECT_PROFILE_PARAMS,
  type EjectProfile,
  type EjectProfileCreate,
  type EjectProfileParams,
} from '../types/ejectProfiles';
import type { ModelGeometry } from '../types/modelGeometries';

/**
 * Shared model-geometry registry query. Drives the model pickers on the
 * preview/dry-run tools and the geometry-derived validation bounds in the
 * profile form. Degrades gracefully: when the query fails, geometry-derived
 * checks are simply skipped (the backend re-validates authoritatively).
 */
function useModelGeometries() {
  return useQuery({
    queryKey: ['model-geometries'],
    queryFn: api.getModelGeometries,
    staleTime: 60_000,
  });
}

/** Option label for a registry model: unvalidated rows are marked so an
 *  operator never mistakes a ladder-pending model for a qualified one. */
function modelOptionLabel(g: ModelGeometry, unvalidatedText: string): string {
  return g.validated ? g.model_key : `${g.model_key} — ${unvalidatedText}`;
}

// Numeric field metadata: the parameter key, its i18n label suffix, and the
// input min/max/step. Kept in one place so the create/edit grid and its
// validation stay in lock-step with the backend contract.
interface NumericFieldMeta {
  key: keyof EjectProfileParams;
  i18n: string;
  min: number;
  max: number;
  step: number;
}

const NUMERIC_FIELDS: NumericFieldMeta[] = [
  { key: 'cooldown_temp_c', i18n: 'cooldownTemp', min: 15, max: 60, step: 0.5 },
  { key: 'cooldown_retries', i18n: 'cooldownRetries', min: 1, max: 30, step: 1 },
  { key: 'clearance_mm', i18n: 'clearance', min: 0, max: 100, step: 0.1 },
  { key: 'z_offset_mm', i18n: 'zOffset', min: 0.4, max: 20, step: 0.1 },
  { key: 'descent_steps', i18n: 'descentSteps', min: 1, max: 50, step: 1 },
  { key: 'x_passes', i18n: 'xPasses', min: 1, max: 99, step: 1 },
  { key: 'x_margin_mm', i18n: 'xMargin', min: 0, max: 100, step: 0.5 },
  { key: 'front_overhang_mm', i18n: 'frontOverhang', min: 0, max: 100, step: 0.5 },
  { key: 'back_overhang_mm', i18n: 'backOverhang', min: 0, max: 100, step: 0.5 },
  { key: 'eject_speed_mm_min', i18n: 'ejectSpeed', min: 100, max: 30000, step: 100 },
  { key: 'skim_speed_mm_min', i18n: 'skimSpeed', min: 100, max: 30000, step: 100 },
  { key: 'max_part_height_mm', i18n: 'maxPartHeight', min: 1, max: 300, step: 1 },
];

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary ' +
  'focus:outline-none focus:ring-2 focus:ring-bambu-green/50 focus:border-bambu-green transition-colors';

/**
 * Small plain-English help affordance rendered beside a field label. Follows the
 * fork's established `title=`-on-an-icon convention (e.g. PrinterSelector) — a
 * native tooltip on hover plus an `aria-label` so assistive tech announces it,
 * with no extra dependency. Kept OUTSIDE the `<label>` element so the field's
 * accessible name stays exactly the visible label text.
 */
function InfoHint({ text }: { text: string }) {
  return (
    <span
      role="img"
      aria-label={text}
      title={text}
      className="inline-flex items-center text-bambu-gray/60 hover:text-bambu-gray transition-colors cursor-help"
    >
      <Info className="w-3.5 h-3.5" aria-hidden="true" />
    </span>
  );
}

// ---------------------------------------------------------------------------
// Create / edit dialog
// ---------------------------------------------------------------------------

interface EjectProfileDialogProps {
  /** Existing profile when editing; null when creating. */
  profile: EjectProfile | null;
  saving: boolean;
  /** Backend failure detail from the last save attempt; rendered inline so a
   *  rejected save never dead-ends silently. */
  error: string | null;
  onSave: (data: EjectProfileCreate) => void;
  onClose: () => void;
}

function EjectProfileDialog({ profile, saving, error, onSave, onClose }: EjectProfileDialogProps) {
  const { t } = useTranslation();
  const isEditing = profile !== null;

  const [name, setName] = useState(profile?.name ?? '');
  // Numeric values are held as strings so decimals type cleanly.
  const [values, setValues] = useState<Record<string, string>>(() => {
    const seed: Record<string, string> = {};
    for (const f of NUMERIC_FIELDS) {
      const current = profile ? profile[f.key] : DEFAULT_EJECT_PROFILE_PARAMS[f.key];
      seed[f.key] = String(current);
    }
    return seed;
  });
  const [fanAssist, setFanAssist] = useState<boolean>(
    profile ? profile.cooling_fan_assist : DEFAULT_EJECT_PROFILE_PARAMS.cooling_fan_assist,
  );
  const [finalSkim, setFinalSkim] = useState<boolean>(
    profile ? profile.final_skim : DEFAULT_EJECT_PROFILE_PARAMS.final_skim,
  );
  // Optional X sweep sub-band: enabled only when both bounds are set. Held as
  // strings like the other numeric inputs so decimals type cleanly.
  const [bandEnabled, setBandEnabled] = useState<boolean>(
    profile ? profile.sweep_x_min_mm != null && profile.sweep_x_max_mm != null : false,
  );
  const [bandMin, setBandMin] = useState<string>(
    profile?.sweep_x_min_mm != null ? String(profile.sweep_x_min_mm) : '',
  );
  const [bandMax, setBandMax] = useState<string>(
    profile?.sweep_x_max_mm != null ? String(profile.sweep_x_max_mm) : '',
  );
  // Sweep start height is edited as a percent (1-100) mapping to frac 0.01-1.0.
  const [startPct, setStartPct] = useState<string>(
    String(Math.round((profile ? profile.sweep_start_frac : DEFAULT_EJECT_PROFILE_PARAMS.sweep_start_frac) * 100)),
  );
  const [nameError, setNameError] = useState(false);

  // Geometry-derived validation bounds (Phase 2): registry rows give the bed
  // width and part-height ceiling; the GET envelope carries the server's
  // minimum sweep-band width so it is never hardcoded here. Settings give the
  // cooldown ambient-trap warn floor. All checks degrade to no-ops while the
  // queries are unavailable — the backend re-validates authoritatively.
  const { data: geoData } = useModelGeometries();
  const { data: appSettings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const geometries = geoData?.geometries;
  const maxBedX =
    geometries && geometries.length > 0 ? Math.max(...geometries.map((g) => g.bed_x)) : null;
  const maxCeiling =
    geometries && geometries.length > 0
      ? Math.max(...geometries.map((g) => g.max_part_height_mm))
      : null;

  // Hard error (mirrors the backend 422): inverted band, sub-minimum width, or
  // a band reaching beyond the widest registered bed. Blocks submit.
  const bandError = (() => {
    if (!bandEnabled) return null;
    const lo = Number(bandMin);
    const hi = Number(bandMax);
    if (bandMin === '' || bandMax === '' || !Number.isFinite(lo) || !Number.isFinite(hi)) {
      return null; // incomplete input — backend rejects a one-sided band
    }
    if (lo >= hi) return t('ejectProfiles.geometry.bandInverted');
    const minWidth = geoData?.sweep_band_min_width_mm;
    if (minWidth != null && hi - lo < minWidth) {
      return t('ejectProfiles.geometry.bandTooNarrow', { min: minWidth });
    }
    if (maxBedX != null && hi > maxBedX) {
      return t('ejectProfiles.geometry.bandExceedsBed', { max: maxBedX });
    }
    return null;
  })();

  // Non-blocking warnings: a profile taller than every registered model's
  // ceiling can never dispatch; a cooldown threshold below the warn floor
  // risks the ambient trap (a wait that can never complete).
  const heightWarning = (() => {
    const h = Number(values.max_part_height_mm);
    if (!Number.isFinite(h) || maxCeiling == null || h <= maxCeiling) return null;
    return t('ejectProfiles.geometry.heightExceedsCeiling', { max: maxCeiling });
  })();
  const cooldownFloor = appSettings?.farm_cooldown_warn_floor_c;
  const cooldownWarning = (() => {
    const c = Number(values.cooldown_temp_c);
    if (!Number.isFinite(c) || cooldownFloor == null || c >= cooldownFloor) return null;
    return t('ejectProfiles.geometry.cooldownAmbientTrap', { floor: cooldownFloor });
  })();

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setNameError(true);
      return;
    }
    if (bandError) {
      return; // hard geometry error — the inline alert explains the block
    }
    const params = {} as EjectProfileParams;
    for (const f of NUMERIC_FIELDS) {
      const parsed = Number(values[f.key]);
      // Fall back to the sane default rather than submitting NaN if the field
      // was cleared; the backend re-validates ranges authoritatively.
      const safe = Number.isFinite(parsed) ? parsed : (DEFAULT_EJECT_PROFILE_PARAMS[f.key] as number);
      (params[f.key] as number) = safe;
    }
    params.cooling_fan_assist = fanAssist;
    params.final_skim = finalSkim;
    // Percent (1-100) -> fraction (0.01-1.0), clamped; backend re-validates.
    const pct = Number(startPct);
    const safePct = Number.isFinite(pct) ? Math.min(100, Math.max(1, pct)) : 100;
    params.sweep_start_frac = safePct / 100;
    // X sub-band: send both bounds only when enabled; otherwise clear to null.
    if (bandEnabled) {
      const lo = Number(bandMin);
      const hi = Number(bandMax);
      params.sweep_x_min_mm = Number.isFinite(lo) ? lo : null;
      params.sweep_x_max_mm = Number.isFinite(hi) ? hi : null;
    } else {
      params.sweep_x_min_mm = null;
      params.sweep_x_max_mm = null;
    }
    onSave({ name: name.trim(), ...params });
  };

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={saving ? undefined : onClose}
      role="dialog"
      aria-modal="true"
      aria-label={isEditing ? t('ejectProfiles.editTitle') : t('ejectProfiles.createTitle')}
    >
      <Card
        className="w-full max-w-2xl max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <PackageOpen className="w-5 h-5 text-bambu-green" />
              <h2 className="text-lg font-semibold text-white">
                {isEditing ? t('ejectProfiles.editTitle') : t('ejectProfiles.createTitle')}
              </h2>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={saving} aria-label={t('common.close')}>
              <X className="w-5 h-5" />
            </Button>
          </div>

          <form onSubmit={handleSubmit} className="p-4 space-y-4">
            {/* Name */}
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <label htmlFor="eject-name" className="block text-sm font-medium text-white">
                  {t('ejectProfiles.fields.name')}
                </label>
                <InfoHint text={t('ejectProfiles.tooltips.name')} />
              </div>
              <input
                id="eject-name"
                type="text"
                maxLength={100}
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  if (nameError) setNameError(false);
                }}
                className={`${inputClass} ${nameError ? 'border-red-500' : ''}`}
                aria-invalid={nameError}
                aria-describedby={nameError ? 'eject-name-error' : undefined}
                autoFocus
              />
              {nameError && (
                <p id="eject-name-error" className="text-red-400 text-xs mt-1">
                  {t('ejectProfiles.nameRequired')}
                </p>
              )}
            </div>

            {/* Numeric fields */}
            <div className="grid gap-3 sm:grid-cols-2">
              {NUMERIC_FIELDS.map((f) => {
                const id = `eject-${f.key}`;
                return (
                  <div key={f.key}>
                    <div className="flex items-center gap-1.5 mb-1">
                      <label htmlFor={id} className="block text-sm text-bambu-gray">
                        {t(`ejectProfiles.fields.${f.i18n}`)}
                      </label>
                      <InfoHint text={t(`ejectProfiles.tooltips.${f.i18n}`)} />
                    </div>
                    <input
                      id={id}
                      type="number"
                      inputMode="decimal"
                      min={f.min}
                      max={f.max}
                      step={f.step}
                      value={values[f.key]}
                      onChange={(e) =>
                        setValues((prev) => ({ ...prev, [f.key]: e.target.value }))
                      }
                      className={inputClass}
                    />
                    {f.key === 'cooldown_temp_c' && cooldownWarning && (
                      <p role="note" className="text-xs text-yellow-300 mt-1">
                        {cooldownWarning}
                      </p>
                    )}
                    {f.key === 'max_part_height_mm' && heightWarning && (
                      <p role="note" className="text-xs text-yellow-300 mt-1">
                        {heightWarning}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>

            {/* Sweep start height (percent of part height) */}
            <div>
              <div className="flex items-center gap-1.5 mb-1">
                <label htmlFor="eject-sweep-start" className="block text-sm text-bambu-gray">
                  {t('ejectProfiles.fields.sweepStartHeight')}
                </label>
                <InfoHint text={t('ejectProfiles.tooltips.sweepStartHeight')} />
              </div>
              <input
                id="eject-sweep-start"
                type="number"
                inputMode="numeric"
                min={1}
                max={100}
                step={1}
                value={startPct}
                onChange={(e) => setStartPct(e.target.value)}
                className={inputClass}
                aria-describedby="eject-sweep-start-help"
              />
              <p id="eject-sweep-start-help" className="text-xs text-bambu-gray mt-1">
                {t('ejectProfiles.fields.sweepStartHeightHelp')}
              </p>
            </div>

            {/* Optional X sweep sub-band */}
            <div>
              <div className="flex items-center justify-between gap-3">
                <span className="flex items-center gap-1.5">
                  <span className="text-sm text-white">{t('ejectProfiles.fields.sweepBand')}</span>
                  <InfoHint text={t('ejectProfiles.tooltips.sweepBand')} />
                </span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={bandEnabled}
                  aria-label={t('ejectProfiles.fields.sweepBand')}
                  onClick={() => setBandEnabled((v) => !v)}
                  className={`relative w-10 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50 ${
                    bandEnabled ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
                  }`}
                >
                  <span
                    className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                      bandEnabled ? 'translate-x-5' : 'translate-x-0.5'
                    }`}
                  />
                </button>
              </div>
              <p className="text-xs text-bambu-gray mt-1">{t('ejectProfiles.fields.sweepBandHelp')}</p>
              {bandEnabled && (
                <div className="grid gap-3 sm:grid-cols-2 mt-3">
                  <div>
                    <div className="flex items-center gap-1.5 mb-1">
                      <label htmlFor="eject-sweep-x-min" className="block text-sm text-bambu-gray">
                        {t('ejectProfiles.fields.sweepXMin')}
                      </label>
                      <InfoHint text={t('ejectProfiles.tooltips.sweepXMin')} />
                    </div>
                    <input
                      id="eject-sweep-x-min"
                      type="number"
                      inputMode="decimal"
                      min={0}
                      step={0.5}
                      value={bandMin}
                      onChange={(e) => setBandMin(e.target.value)}
                      className={inputClass}
                    />
                  </div>
                  <div>
                    <div className="flex items-center gap-1.5 mb-1">
                      <label htmlFor="eject-sweep-x-max" className="block text-sm text-bambu-gray">
                        {t('ejectProfiles.fields.sweepXMax')}
                      </label>
                      <InfoHint text={t('ejectProfiles.tooltips.sweepXMax')} />
                    </div>
                    <input
                      id="eject-sweep-x-max"
                      type="number"
                      inputMode="decimal"
                      min={0}
                      step={0.5}
                      value={bandMax}
                      onChange={(e) => setBandMax(e.target.value)}
                      className={inputClass}
                    />
                  </div>
                </div>
              )}
              {bandError && (
                <p role="alert" className="text-xs text-red-400 mt-2">
                  {bandError}
                </p>
              )}
            </div>

            {/* Cooling fan assist toggle */}
            <div className="flex items-center justify-between gap-3">
              <span className="flex items-center gap-1.5">
                <span className="text-sm text-white">{t('ejectProfiles.fields.coolingFanAssist')}</span>
                <InfoHint text={t('ejectProfiles.tooltips.coolingFanAssist')} />
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={fanAssist}
                aria-label={t('ejectProfiles.fields.coolingFanAssist')}
                onClick={() => setFanAssist((v) => !v)}
                className={`relative w-10 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50 ${
                  fanAssist ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
                }`}
              >
                <span
                  className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                    fanAssist ? 'translate-x-5' : 'translate-x-0.5'
                  }`}
                />
              </button>
            </div>

            {/* Final skim pass toggle */}
            <div>
              <div className="flex items-center justify-between gap-3">
                <span className="flex items-center gap-1.5">
                  <span className="text-sm text-white">{t('ejectProfiles.fields.finalSkim')}</span>
                  <InfoHint text={t('ejectProfiles.tooltips.finalSkim')} />
                </span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={finalSkim}
                  aria-label={t('ejectProfiles.fields.finalSkim')}
                  onClick={() => setFinalSkim((v) => !v)}
                  className={`relative w-10 h-5 rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50 ${
                    finalSkim ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
                  }`}
                >
                  <span
                    className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                      finalSkim ? 'translate-x-5' : 'translate-x-0.5'
                    }`}
                  />
                </button>
              </div>
              <p className="text-xs text-bambu-gray mt-1">{t('ejectProfiles.fields.finalSkimHelp')}</p>
            </div>

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
                    <PackageOpen className="w-4 h-4" />
                    {t('common.save')}
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
// Preview & validate panel
// ---------------------------------------------------------------------------

function PreviewPanel({ profiles }: { profiles: EjectProfile[] }) {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [profileId, setProfileId] = useState<number | null>(profiles[0]?.id ?? null);
  const [fileId, setFileId] = useState<number | null>(null);
  const [plateIndex, setPlateIndex] = useState<number>(1);
  const [showGcode, setShowGcode] = useState(false);

  // Geometry target: the backend resolves bed/envelope from a model key.
  // Defaults to the first hardware-validated registry row; unvalidated models
  // stay selectable (ladder tools) and are marked in the option label.
  const { data: geoData } = useModelGeometries();
  const geometries = useMemo(() => geoData?.geometries ?? [], [geoData]);
  const [modelChoice, setModelChoice] = useState<string | null>(null);
  const model =
    modelChoice ?? geometries.find((g) => g.validated)?.model_key ?? geometries[0]?.model_key ?? null;

  // Only 3MF files carry the plate/slice metadata the generator needs.
  const { data: files } = useQuery({
    queryKey: ['library-files', 'eject-preview'],
    queryFn: () => api.getLibraryFiles(),
  });
  const previewableFiles = useMemo(
    () => (files ?? []).filter((f) => f.filename.toLowerCase().endsWith('.3mf')),
    [files],
  );

  const { data: platesData } = useQuery({
    queryKey: ['library-file-plates', fileId],
    queryFn: () => api.getLibraryFilePlates(fileId!),
    enabled: fileId !== null,
  });
  const plates = useMemo(() => platesData?.plates ?? [], [platesData]);
  usePlateIndexSync(plates, plateIndex, setPlateIndex);

  const previewMutation = useMutation({
    mutationFn: () =>
      api.previewEjectProfile(profileId!, {
        library_file_id: fileId!,
        plate_index: plateIndex,
        model: model!,
      }),
    onError: (err: Error) => {
      showToast(err.message || t('ejectProfiles.preview.failed'), 'error');
    },
  });

  const result = previewMutation.data;
  const canPreview =
    profileId !== null && fileId !== null && model !== null && !previewMutation.isPending;

  return (
    <Card className="mt-6">
      <CardContent>
        <h2 className="text-lg font-semibold text-white mb-1">{t('ejectProfiles.preview.title')}</h2>
        <p className="text-sm text-bambu-gray mb-4">{t('ejectProfiles.preview.description')}</p>

        {previewableFiles.length === 0 ? (
          <p className="text-sm text-bambu-gray italic">{t('ejectProfiles.preview.noFiles')}</p>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {/* Profile */}
              <div>
                <label htmlFor="preview-profile" className="block text-sm text-bambu-gray mb-1">
                  {t('ejectProfiles.title')}
                </label>
                <select
                  id="preview-profile"
                  value={profileId ?? ''}
                  onChange={(e) => setProfileId(Number(e.target.value))}
                  className={inputClass}
                >
                  {profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>

              {/* Geometry model */}
              <div>
                <label htmlFor="preview-model" className="block text-sm text-bambu-gray mb-1">
                  {t('ejectProfiles.geometry.model')}
                </label>
                <select
                  id="preview-model"
                  value={model ?? ''}
                  onChange={(e) => setModelChoice(e.target.value || null)}
                  className={inputClass}
                  disabled={geometries.length === 0}
                >
                  {geometries.length === 0 ? (
                    <option value="">{t('ejectProfiles.geometry.noModels')}</option>
                  ) : (
                    geometries.map((g) => (
                      <option key={g.model_key} value={g.model_key}>
                        {modelOptionLabel(g, t('ejectProfiles.geometry.unvalidated'))}
                      </option>
                    ))
                  )}
                </select>
              </div>

              {/* Library file */}
              <div>
                <label htmlFor="preview-file" className="block text-sm text-bambu-gray mb-1">
                  {t('ejectProfiles.preview.file')}
                </label>
                <select
                  id="preview-file"
                  value={fileId ?? ''}
                  onChange={(e) => {
                    const next = e.target.value ? Number(e.target.value) : null;
                    setFileId(next);
                    setPlateIndex(1);
                    previewMutation.reset();
                  }}
                  className={inputClass}
                >
                  <option value="">{t('ejectProfiles.preview.filePlaceholder')}</option>
                  {previewableFiles.map((f) => (
                    <option key={f.id} value={f.id}>
                      {f.filename}
                    </option>
                  ))}
                </select>
              </div>

              {/* Plate */}
              <div>
                <label htmlFor="preview-plate" className="block text-sm text-bambu-gray mb-1">
                  {t('ejectProfiles.preview.plate')}
                </label>
                <select
                  id="preview-plate"
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
            </div>

            <div className="mt-4">
              <Button onClick={() => previewMutation.mutate()} disabled={!canPreview}>
                {previewMutation.isPending ? (
                  <>
                    <Loader2 className="w-4 h-4 animate-spin" />
                    {t('ejectProfiles.preview.running')}
                  </>
                ) : (
                  t('ejectProfiles.preview.run')
                )}
              </Button>
            </div>

            {/* Result */}
            {result && (
              <div className="mt-4 space-y-3">
                {result.validation.ok && result.validation.errors.length === 0 ? (
                  <div className="flex items-center gap-2 p-3 bg-bambu-green/10 border border-bambu-green/30 rounded-lg text-sm text-bambu-green">
                    <PackageOpen className="w-4 h-4 flex-shrink-0" />
                    <span>{t('ejectProfiles.preview.ok')}</span>
                  </div>
                ) : null}

                {/* Geometry warnings (independent of G-code validation) — e.g.
                    the chosen model's geometry is not hardware-validated yet. */}
                {(result.warnings ?? []).length > 0 && (
                  <div className="p-3 bg-yellow-500/10 border border-yellow-500/40 rounded-lg">
                    <div className="flex items-center gap-2 text-sm font-medium text-yellow-400 mb-1">
                      <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                      {t('ejectProfiles.geometry.warningsTitle')}
                    </div>
                    <ul className="list-disc pl-6 text-sm text-yellow-200 space-y-0.5">
                      {(result.warnings ?? []).map((msg, i) => (
                        <li key={i}>{msg}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {result.validation.errors.length > 0 && (
                  <div className="p-3 bg-red-500/10 border border-red-500/40 rounded-lg">
                    <div className="flex items-center gap-2 text-sm font-medium text-red-400 mb-1">
                      <AlertCircle className="w-4 h-4 flex-shrink-0" />
                      {t('ejectProfiles.preview.errors')}
                    </div>
                    <ul className="list-disc pl-6 text-sm text-red-300 space-y-0.5">
                      {result.validation.errors.map((msg, i) => (
                        <li key={i}>{msg}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {result.validation.warnings.length > 0 && (
                  <div className="p-3 bg-yellow-500/10 border border-yellow-500/40 rounded-lg">
                    <div className="flex items-center gap-2 text-sm font-medium text-yellow-400 mb-1">
                      <AlertTriangle className="w-4 h-4 flex-shrink-0" />
                      {t('ejectProfiles.preview.warnings')}
                    </div>
                    <ul className="list-disc pl-6 text-sm text-yellow-200 space-y-0.5">
                      {result.validation.warnings.map((msg, i) => (
                        <li key={i}>{msg}</li>
                      ))}
                    </ul>
                  </div>
                )}

                <p className="text-sm text-bambu-gray">
                  {t('ejectProfiles.preview.maxZ', { value: result.max_z_height })}
                </p>

                {/* Collapsible G-code viewer */}
                <div>
                  <button
                    type="button"
                    onClick={() => setShowGcode((v) => !v)}
                    aria-expanded={showGcode}
                    className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50 rounded"
                  >
                    {showGcode ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
                    {showGcode ? t('ejectProfiles.preview.hideGcode') : t('ejectProfiles.preview.showGcode')}
                  </button>
                  {showGcode && (
                    <pre
                      aria-label={t('ejectProfiles.preview.gcodeLabel')}
                      className="mt-2 p-3 bg-bambu-dark rounded-lg text-xs font-mono text-bambu-gray-light whitespace-pre overflow-auto max-h-96"
                    >
                      {result.gcode}
                    </pre>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Dry-run dispatch / download dialog
// ---------------------------------------------------------------------------

interface DryRunDialogProps {
  profile: EjectProfile;
  onClose: () => void;
}

/**
 * Per-profile dry-run test. Two exits: dispatch a geometry-only eject test onto
 * a connected printer (hardware-ladder step 1 — the bed MUST be empty), or
 * download the dry-run 3MF to inspect it. File/plate come from the same library
 * sources the preview card uses; printers come from the shared printers query,
 * filtered to those whose live status reports connected.
 */
function DryRunDialog({ profile, onClose }: DryRunDialogProps) {
  const { t } = useTranslation();

  const [printerId, setPrinterId] = useState<number | null>(null);
  const [fileId, setFileId] = useState<number | null>(null);
  const [plateIndex, setPlateIndex] = useState<number>(1);
  // Download geometry target: '' = use the selected printer's model (the
  // backend resolves it from printer_id); an explicit registry key lets the
  // operator build e.g. an H2C ladder file with no H2C printer connected.
  const [downloadModel, setDownloadModel] = useState<string>('');
  const { data: geoData } = useModelGeometries();
  const geometries = useMemo(() => geoData?.geometries ?? [], [geoData]);

  // Printers: base list from the shared query, connectivity from live status
  // (the Printer entity itself carries no connection flag — same approach as
  // PrinterSelector). Only offer printers whose status reports connected.
  const { data: printers } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const activePrinters = useMemo(() => (printers ?? []).filter((p) => p.is_active), [printers]);
  const statusQueries = useQueries({
    queries: activePrinters.map((p) => ({
      queryKey: ['printerStatus', p.id],
      queryFn: () => api.getPrinterStatus(p.id),
      staleTime: 5000,
    })),
  });
  const statusesLoading = activePrinters.length > 0 && statusQueries.some((q) => q.isLoading);
  const connectedPrinters = useMemo(
    () => activePrinters.filter((_, i) => statusQueries[i]?.data?.connected === true),
    [activePrinters, statusQueries],
  );

  // Only 3MF files carry the plate/slice metadata the generator needs — same
  // filter + query key as the preview card so the data is shared.
  const { data: files } = useQuery({
    queryKey: ['library-files', 'eject-preview'],
    queryFn: () => api.getLibraryFiles(),
  });
  const previewableFiles = useMemo(
    () => (files ?? []).filter((f) => f.filename.toLowerCase().endsWith('.3mf')),
    [files],
  );

  const { data: platesData } = useQuery({
    queryKey: ['library-file-plates', fileId],
    queryFn: () => api.getLibraryFilePlates(fileId!),
    enabled: fileId !== null,
  });
  const plates = useMemo(() => platesData?.plates ?? [], [platesData]);
  usePlateIndexSync(plates, plateIndex, setPlateIndex);

  const dispatchMutation = useMutation({
    mutationFn: () =>
      api.dispatchEjectProfileDryRun(profile.id, {
        library_file_id: fileId!,
        plate_index: plateIndex,
        printer_id: printerId!,
      }),
  });

  const downloadMutation = useMutation({
    mutationFn: () =>
      api.downloadEjectProfileDryRun(
        profile.id,
        downloadModel
          ? { library_file_id: fileId!, plate_index: plateIndex, model: downloadModel }
          : { library_file_id: fileId!, plate_index: plateIndex, printer_id: printerId! },
        `dryrun_${profile.name}.gcode.3mf`,
      ),
  });

  const busy = dispatchMutation.isPending || downloadMutation.isPending;
  const canDispatch = printerId !== null && fileId !== null && !busy;
  const canDownload = fileId !== null && (downloadModel !== '' || printerId !== null) && !busy;

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={busy ? undefined : onClose}
      role="dialog"
      aria-modal="true"
      aria-label={t('ejectProfiles.dryRun.title')}
    >
      <Card className="w-full max-w-lg max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <CardContent className="p-0">
          <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
            <div className="flex items-center gap-2">
              <FlaskConical className="w-5 h-5 text-bambu-green" />
              <div>
                <h2 className="text-lg font-semibold text-white">{t('ejectProfiles.dryRun.title')}</h2>
                <p className="text-xs text-bambu-gray">{profile.name}</p>
              </div>
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} disabled={busy} aria-label={t('common.close')}>
              <X className="w-5 h-5" />
            </Button>
          </div>

          <div className="p-4 space-y-4">
            <p className="text-sm text-bambu-gray">{t('ejectProfiles.dryRun.description')}</p>

            {/* EMPTY-BED warning — always visible, never behind a toast. */}
            <div
              role="alert"
              className="flex items-start gap-2 p-3 bg-yellow-500/10 border border-yellow-500/40 rounded-lg text-sm text-yellow-200"
            >
              <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5 text-yellow-400" />
              <span>
                <span className="font-semibold text-yellow-300">{t('ejectProfiles.dryRun.warningTitle')}: </span>
                {t('ejectProfiles.dryRun.warning')}
              </span>
            </div>

            {/* Printer */}
            <div>
              <label htmlFor="dryrun-printer" className="block text-sm text-bambu-gray mb-1">
                {t('ejectProfiles.dryRun.printer')}
              </label>
              <select
                id="dryrun-printer"
                value={printerId ?? ''}
                onChange={(e) => setPrinterId(e.target.value ? Number(e.target.value) : null)}
                className={inputClass}
                disabled={statusesLoading || connectedPrinters.length === 0}
              >
                <option value="">{t('ejectProfiles.dryRun.printerPlaceholder')}</option>
                {connectedPrinters.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                    {p.model ? ` (${p.model})` : ''}
                  </option>
                ))}
              </select>
              {statusesLoading ? (
                <p className="text-xs text-bambu-gray mt-1">{t('ejectProfiles.dryRun.loadingPrinters')}</p>
              ) : connectedPrinters.length === 0 ? (
                <p className="text-xs text-yellow-300 mt-1">{t('ejectProfiles.dryRun.noPrinters')}</p>
              ) : null}
            </div>

            {/* Download geometry model (optional) — the file download resolves
                geometry from this key instead of the selected printer, so a
                ladder file can be built for a model with no printer connected. */}
            <div>
              <label htmlFor="dryrun-model" className="block text-sm text-bambu-gray mb-1">
                {t('ejectProfiles.geometry.downloadModel')}
              </label>
              <select
                id="dryrun-model"
                value={downloadModel}
                onChange={(e) => setDownloadModel(e.target.value)}
                className={inputClass}
                disabled={geometries.length === 0}
              >
                <option value="">{t('ejectProfiles.geometry.downloadModelPlaceholder')}</option>
                {geometries.map((g) => (
                  <option key={g.model_key} value={g.model_key}>
                    {modelOptionLabel(g, t('ejectProfiles.geometry.unvalidated'))}
                  </option>
                ))}
              </select>
            </div>

            {/* Library file */}
            <div>
              <label htmlFor="dryrun-file" className="block text-sm text-bambu-gray mb-1">
                {t('ejectProfiles.preview.file')}
              </label>
              <select
                id="dryrun-file"
                value={fileId ?? ''}
                onChange={(e) => {
                  setFileId(e.target.value ? Number(e.target.value) : null);
                  setPlateIndex(1);
                  dispatchMutation.reset();
                  downloadMutation.reset();
                }}
                className={inputClass}
              >
                <option value="">{t('ejectProfiles.preview.filePlaceholder')}</option>
                {previewableFiles.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.filename}
                  </option>
                ))}
              </select>
              {previewableFiles.length === 0 && (
                <p className="text-xs text-bambu-gray mt-1">{t('ejectProfiles.preview.noFiles')}</p>
              )}
            </div>

            {/* Plate */}
            <div>
              <label htmlFor="dryrun-plate" className="block text-sm text-bambu-gray mb-1">
                {t('ejectProfiles.preview.plate')}
              </label>
              <select
                id="dryrun-plate"
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

            {/* Dispatch success — persistent inline (returned message). */}
            {dispatchMutation.data && (
              <div
                role="status"
                className="flex items-start gap-2 p-3 bg-bambu-green/10 border border-bambu-green/30 rounded-lg text-sm text-bambu-green"
              >
                <FlaskConical className="w-4 h-4 flex-shrink-0 mt-0.5" />
                <span>{dispatchMutation.data.message}</span>
              </div>
            )}

            {/* Dispatch failure — persistent inline alert. */}
            {dispatchMutation.error && (
              <div
                role="alert"
                className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300"
              >
                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
                <span>{dispatchMutation.error.message || t('ejectProfiles.dryRun.dispatchFailed')}</span>
              </div>
            )}

            {/* Download failure — persistent inline alert. */}
            {downloadMutation.error && (
              <div
                role="alert"
                className="flex items-start gap-2 p-3 bg-red-500/10 border border-red-500/40 rounded-lg text-sm text-red-300"
              >
                <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5 text-red-400" />
                <span>{downloadMutation.error.message || t('ejectProfiles.dryRun.downloadFailed')}</span>
              </div>
            )}

            {/* Actions */}
            <div className="flex flex-col sm:flex-row gap-3 pt-1">
              <Button
                type="button"
                variant="secondary"
                onClick={() => downloadMutation.mutate()}
                disabled={!canDownload}
                className="flex-1"
              >
                {downloadMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Download className="w-4 h-4" />
                )}
                {downloadMutation.isPending ? t('ejectProfiles.dryRun.downloading') : t('ejectProfiles.dryRun.download')}
              </Button>
              <Button
                type="button"
                onClick={() => dispatchMutation.mutate()}
                disabled={!canDispatch}
                className="flex-1"
              >
                {dispatchMutation.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Send className="w-4 h-4" />
                )}
                {dispatchMutation.isPending ? t('ejectProfiles.dryRun.dispatching') : t('ejectProfiles.dryRun.dispatch')}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function EjectProfilesPage() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [dialogState, setDialogState] = useState<{ open: boolean; profile: EjectProfile | null }>({
    open: false,
    profile: null,
  });
  const [pendingDelete, setPendingDelete] = useState<EjectProfile | null>(null);
  const [dryRunProfile, setDryRunProfile] = useState<EjectProfile | null>(null);

  const {
    data: profiles,
    isLoading,
    isError,
    refetch,
    isFetching,
  } = useQuery({
    queryKey: ['eject-profiles'],
    queryFn: api.getEjectProfiles,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['eject-profiles'] });

  const saveMutation = useMutation({
    mutationFn: (data: EjectProfileCreate) => {
      const editingId = dialogState.profile?.id;
      return editingId != null
        ? api.updateEjectProfile(editingId, data)
        : api.createEjectProfile(data);
    },
    onSuccess: () => {
      const editing = dialogState.profile != null;
      showToast(editing ? t('ejectProfiles.updated') : t('ejectProfiles.created'));
      invalidate();
      setDialogState({ open: false, profile: null });
    },
    // No error toast: the failure detail renders inline inside the open
    // dialog (EjectProfileDialog `error` prop).
  });

  const openDialog = (profile: EjectProfile | null) => {
    // Clear any error left over from a previous failed attempt so the dialog
    // opens clean.
    saveMutation.reset();
    setDialogState({ open: true, profile });
  };

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteEjectProfile(id),
    onSuccess: () => {
      showToast(t('ejectProfiles.deleted'));
      invalidate();
      setPendingDelete(null);
    },
    onError: (err: Error) => {
      showToast(err.message || t('ejectProfiles.deleteFailed'), 'error');
      setPendingDelete(null);
    },
  });

  const list = profiles ?? [];

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            <PackageOpen className="w-6 h-6 text-bambu-green" />
            {t('ejectProfiles.title')}
          </h1>
          <p className="text-sm text-bambu-gray mt-1 max-w-2xl">{t('ejectProfiles.description')}</p>
        </div>
        {list.length > 0 && (
          <Button onClick={() => openDialog(null)}>
            <Plus className="w-4 h-4" />
            {t('ejectProfiles.newProfile')}
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
            <p className="text-white mb-4">{t('ejectProfiles.loadError')}</p>
            <Button variant="secondary" onClick={() => refetch()} disabled={isFetching}>
              {isFetching ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
              {t('common.retry')}
            </Button>
          </CardContent>
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center text-center py-16">
            <PackageOpen className="w-12 h-12 text-bambu-gray mb-4" />
            <h2 className="text-lg font-semibold text-white mb-1">{t('ejectProfiles.empty.title')}</h2>
            <p className="text-sm text-bambu-gray mb-5 max-w-md">{t('ejectProfiles.empty.body')}</p>
            <Button onClick={() => openDialog(null)}>
              <Plus className="w-4 h-4" />
              {t('ejectProfiles.newProfile')}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-bambu-gray text-left border-b border-bambu-dark-tertiary">
                <tr>
                  <th className="py-3 px-4 font-medium">{t('ejectProfiles.fields.name')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('ejectProfiles.columns.cooldownTemp')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('ejectProfiles.columns.retries')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('ejectProfiles.columns.passes')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('ejectProfiles.columns.ejectSpeed')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('ejectProfiles.columns.maxHeight')}</th>
                  <th className="py-3 px-4 font-medium text-right">{t('common.actions')}</th>
                </tr>
              </thead>
              <tbody>
                {list.map((p) => (
                  <tr key={p.id} className="border-b border-bambu-dark-tertiary last:border-b-0">
                    <td className="py-3 px-4 text-white font-medium">{p.name}</td>
                    <td className="py-3 px-4 text-bambu-gray text-right">{p.cooldown_temp_c}</td>
                    <td className="py-3 px-4 text-bambu-gray text-right">{p.cooldown_retries}</td>
                    <td className="py-3 px-4 text-bambu-gray text-right">{p.x_passes}</td>
                    <td className="py-3 px-4 text-bambu-gray text-right">{p.eject_speed_mm_min}</td>
                    <td className="py-3 px-4 text-bambu-gray text-right">{p.max_part_height_mm}</td>
                    <td className="py-3 px-4">
                      <div className="flex items-center justify-end gap-1">
                        <button
                          type="button"
                          onClick={() => setDryRunProfile(p)}
                          className="p-2 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50"
                          aria-label={`${t('ejectProfiles.dryRun.action')} ${p.name}`}
                          title={t('ejectProfiles.dryRun.action')}
                        >
                          <FlaskConical className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => openDialog(p)}
                          className="p-2 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors focus:outline-none focus:ring-2 focus:ring-bambu-green/50"
                          aria-label={`${t('common.edit')} ${p.name}`}
                          title={t('common.edit')}
                        >
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button
                          type="button"
                          onClick={() => setPendingDelete(p)}
                          className="p-2 rounded text-red-400 hover:text-red-300 hover:bg-bambu-dark-tertiary transition-colors focus:outline-none focus:ring-2 focus:ring-red-500/50"
                          aria-label={`${t('common.delete')} ${p.name}`}
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

      {/* Preview panel only makes sense once at least one profile exists. */}
      {list.length > 0 && <PreviewPanel profiles={list} />}

      {dialogState.open && (
        <EjectProfileDialog
          profile={dialogState.profile}
          saving={saveMutation.isPending}
          error={
            saveMutation.error
              ? saveMutation.error.message || t('ejectProfiles.saveFailed')
              : null
          }
          onSave={(data) => saveMutation.mutate(data)}
          onClose={() => setDialogState({ open: false, profile: null })}
        />
      )}

      {dryRunProfile && (
        <DryRunDialog profile={dryRunProfile} onClose={() => setDryRunProfile(null)} />
      )}

      {pendingDelete && (
        <ConfirmModal
          title={t('ejectProfiles.deleteTitle')}
          message={t('ejectProfiles.deleteBody', { name: pendingDelete.name })}
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

export default EjectProfilesPage;
