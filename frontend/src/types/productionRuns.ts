/**
 * Production run types (farm production, Phase 2).
 *
 * A production run dispatches copies of a SKU's file/plate across one or more
 * printers until a target unit count is met. The backend owns scheduling and
 * progress accounting; the frontend starts runs and renders live progress.
 *
 * Field names and units mirror the backend contract exactly — do not rename
 * without updating the backend production-run schema.
 */

/** Lifecycle state of a run. */
export type ProductionRunStatus = 'active' | 'paused' | 'completed' | 'cancelled';

/**
 * First-article approval gate state.
 *
 * A first-article run prints one part without auto-eject and holds
 * (`awaiting_approval`) for an operator to inspect before the remaining plates
 * dispatch. `null` when the run does not require first-article approval.
 */
export type FirstArticleState =
  | 'pending_print'
  | 'awaiting_approval'
  | 'approved'
  | 'rejected'
  | null;

/** A printer assigned to a run (id + display name only). */
export interface ProductionRunPrinter {
  id: number;
  name: string;
}

/**
 * Why a run is currently held (Phase 4.1). `operator` = manual pause,
 * `operator_stop` = a unit was deliberately stopped (run stays active but
 * holds that printer), `first_article_rejected`, `no_available_printers` and
 * `retries_exhausted` (a unit failed with no retries left and no work in
 * flight — resume mints replacement plates) are the automatic pauses. `null`
 * when not held.
 */
export type RunPauseReason =
  | 'operator'
  | 'operator_stop'
  | 'first_article_rejected'
  | 'no_available_printers'
  | 'retries_exhausted';

/**
 * Live blocked-state summary for one printer a run targets. Only present on
 * the run DETAIL response (`GET /production-runs/{id}`).
 */
export interface RunPrinterState {
  printer_id: number;
  name: string;
  connected: boolean;
  quarantined: boolean;
  awaiting_plate_clear: boolean;
  model_mismatch: boolean;
  model_mismatch_reason: string | null;
  /** A unit on this printer is flagged printer_offline_stalled. */
  stalled: boolean;
  /** The printer's own pre-print vision check found objects on the bed. */
  vision_hold: boolean;
}

/** One queue item of a run, as shown on the run detail page. */
export interface RunUnit {
  id: number;
  status: 'pending' | 'printing' | 'completed' | 'failed' | 'skipped' | 'cancelled';
  /** 'operator_ui' / 'operator_screen' when deliberately stopped; else null. */
  stop_source: string | null;
  waiting_reason: string | null;
  printer_id: number | null;
  printer_name: string | null;
  started_at: string | null;
  completed_at: string | null;
  /** Retry lineage: the failed unit this one re-covers, and the chain depth. */
  retry_of_id: number | null;
  retry_count: number;
  filament_short: boolean;
  manual_start: boolean;
  first_article: boolean;
  error_message: string | null;
}

/** A persisted production run as returned by the API. */
export interface ProductionRun {
  id: number;
  name: string;
  sku_code: string;
  sku_file_id: number;
  /** Units the operator asked for. */
  target_units: number;
  /** Units the run will actually produce (whole plates ≥ target). */
  units_planned: number;
  units_completed: number;
  units_failed: number;
  plates_total: number;
  plates_completed: number;
  plates_failed: number;
  plates_pending: number;
  status: ProductionRunStatus;
  /** Why the run is held; null when not held (Phase 4.1). */
  pause_reason: RunPauseReason | null;
  /** Pending units staged by the low-spool guard (swap spool, then release). */
  staged_filament_short: number;
  /** Pending units staged for any other reason (pause / manual staging). */
  staged_other: number;
  /** True when any printer the run targets is blocked (quarantine, plate gate,
   *  model mismatch, offline stall, vision hold, or connection lost). */
  has_blocked_printers: boolean;
  /** Per-printer blocked states — detail response only, null on the list. */
  printer_states?: RunPrinterState[] | null;
  /** Per-unit list — detail response only, null on the list. */
  units?: RunUnit[] | null;
  /** Whether this run gates on first-article approval before mass dispatch. */
  require_first_article: boolean;
  /** First-article gate state, or null when the run does not require it. */
  first_article_state: FirstArticleState;
  /** Automatic retries per plate before a plate is marked failed (0–5). */
  retry_max_per_unit: number;
  /** Consecutive per-printer failures that trip an automatic quarantine (1–10). */
  escalate_consecutive_failures: number;
  /** Operator's rejection reason; present when first_article_state is 'rejected'. */
  first_article_reject_reason?: string | null;
  /** Relative URL of the first article's newest finish photo — populated only
   *  while awaiting_approval or rejected; null otherwise (Phase 4, F1). */
  first_article_photo_url?: string | null;
  /** The printer that produced the first article (for the inline camera view);
   *  populated only while awaiting_approval or rejected. */
  first_article_printer_id?: number | null;
  first_article_printer_name?: string | null;
  /** Eject profile the run's plates use (uniform per run), or null. Seeds the
   *  "Run again" dialog (Phase 5, F9). */
  eject_profile_id: number | null;
  /** Per-run cooldown override (°C) applied to eject generation, or null. */
  cooldown_temp_c_override: number | null;
  /** Target printer model for a model-targeted run, or null for a
   *  specific-printers run (its printers are in `printers`). */
  target_model: string | null;
  /** Estimated seconds remaining, or null when not computable. */
  eta_seconds: number | null;
  printers: ProductionRunPrinter[];
  created_at: string;
}

/**
 * Seed values for re-opening the start dialog from a finished run ("Run again",
 * Phase 5, F9). Carries the run's SKU file plus the printer strategy and policy
 * overrides so a repeat run needs no re-entry. The dialog resolves the owning
 * SKU from `skuFileId` via its skus query; if the file was since deleted it
 * falls back to the unprefilled dialog (no dead end).
 */
export interface RunPrefill {
  skuFileId: number;
  targetUnits: number;
  mode: 'specific' | 'model';
  printerIds: number[];
  targetModel: string | null;
  ejectProfileId: number | null;
  cooldownOverride: number | null;
  requireFirstArticle: boolean;
  retryMaxPerUnit: number;
  escalateConsecutiveFailures: number;
}

/**
 * Fleet-scoped "why is this printer on farm work" context (Phase 3, F2).
 *
 * One entry per printer assigned to an active/paused production run, surfaced on
 * the Printers page (`GET /production-runs/printer-states`) so an operator sees
 * why a printer is blocked/idle without opening the run detail. Field names and
 * nullability mirror the backend `FarmPrinterContext` schema exactly.
 */
export interface FarmPrinterContext {
  printer_id: number;
  run_id: number;
  run_name: string;
  sku_code: string | null;
  run_status: ProductionRunStatus;
  pause_reason: RunPauseReason | null;
  /** The printer's representative unit id, or null when it holds none. */
  unit_id: number | null;
  /** 'printing' | 'pending' | 'failed', or null when the printer holds no unit. */
  unit_status: 'printing' | 'pending' | 'failed' | null;
  /** Machine code from the live unit; map with the shared waitingReason util. */
  waiting_reason: string | null;
  /** Last failed unit's error — present ONLY when there is no live unit. */
  error_message: string | null;
  /** A live pending unit held by the scheduler/operator (manual_start). */
  staged: boolean;
  /** The hold is the low-spool guard (swap the spool, then release/resume). */
  filament_short: boolean;
  /** This printer holds the run's first-article unit. */
  first_article: boolean;
  first_article_state: FirstArticleState;
}

/** Body for POST /production-runs/{id}/first-article/approve. */
export interface FirstArticleApproveRequest {
  /** When true the toolhead sweeps the part off the bed; when false the
   *  operator has already removed it by hand. */
  eject_remotely: boolean;
}

/** Body for POST /production-runs/{id}/first-article/reject. */
export interface FirstArticleRejectRequest {
  /** Free-text rejection reason (1..500 chars, enforced by the backend). */
  reason: string;
}

/**
 * Create payload for POST /production-runs.
 *
 * Exactly one printer-target strategy is sent: either an explicit `printer_ids`
 * list, or a `target_model` (e.g. "H2S") to let the scheduler pick any idle
 * printer of that model.
 */
export interface ProductionRunCreate {
  sku_file_id: number;
  target_units: number;
  printer_ids?: number[];
  target_model?: string;
  eject_profile_id?: number | null;
  cooldown_temp_c_override?: number | null;
  /** Gate the run on first-article approval. Defaults to true server-side. */
  require_first_article?: boolean;
  /** Automatic retries per plate before a plate is marked failed (0–5). */
  retry_max_per_unit?: number;
  /** Consecutive per-printer failures that trip a quarantine (1–10). */
  escalate_consecutive_failures?: number;
}
