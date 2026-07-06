/**
 * Eject profile types (farm auto part-removal, Phase 1).
 *
 * An eject profile parameterises the end-of-print G-code that sweeps a finished
 * part off the bed once it has cooled. The backend owns the G-code generator;
 * the frontend only reads/writes the parameter set and renders a preview.
 *
 * Field names and units mirror the backend contract exactly — do not rename
 * without updating `backend/app/schemas/eject_profile.py`.
 */

/** The numeric/boolean parameter set shared by create, update, and read. */
export interface EjectProfileParams {
  /** Bed temperature (°C) the part must cool below before the sweep runs. */
  cooldown_temp_c: number;
  /** How many times to re-poll the bed temperature before giving up. */
  cooldown_retries: number;
  /** Vertical clearance (mm) the toolhead keeps above the part while sweeping. */
  clearance_mm: number;
  /** Z offset (mm) applied to the sweep pass height. */
  z_offset_mm: number;
  /** Number of stepped descents used to hook the part. */
  descent_steps: number;
  /** Number of X sweep passes. */
  x_passes: number;
  /** X margin (mm) kept from the bed edge on each pass. */
  x_margin_mm: number;
  /** Front overhang (mm) of the sweep beyond the part. */
  front_overhang_mm: number;
  /** Back overhang (mm) of the sweep beyond the part. */
  back_overhang_mm: number;
  /** Feedrate (mm/min) for the ejecting passes. */
  eject_speed_mm_min: number;
  /** Feedrate (mm/min) for the skim passes. */
  skim_speed_mm_min: number;
  /** Whether to run the part-cooling fan during the sweep. */
  cooling_fan_assist: boolean;
  /** Append the final slow skim pass after the descent sweeps; false pushes once. */
  final_skim: boolean;
  /** Maximum part height (mm) this profile is validated for. */
  max_part_height_mm: number;
  /** Lower X bound (mm) of an optional sweep sub-band; null = full-width sweep. */
  sweep_x_min_mm: number | null;
  /** Upper X bound (mm) of an optional sweep sub-band; null = full-width sweep. */
  sweep_x_max_mm: number | null;
  /** Fraction (0-1] of the part height the descending sweep starts at (1 = top). */
  sweep_start_frac: number;
}

/** A persisted eject profile as returned by the API. */
export interface EjectProfile extends EjectProfileParams {
  id: number;
  name: string;
  created_at: string;
  updated_at: string;
}

/** Create payload: name + all parameters (id/timestamps assigned server-side). */
export interface EjectProfileCreate extends EjectProfileParams {
  name: string;
}

/** Update payload: identical shape to create (full replace via PUT). */
export type EjectProfileUpdate = EjectProfileCreate;

/** Backend default parameter set, prefilled into the create form. */
export const DEFAULT_EJECT_PROFILE_PARAMS: EjectProfileParams = {
  cooldown_temp_c: 28,
  cooldown_retries: 5,
  clearance_mm: 10,
  z_offset_mm: 0.4,
  descent_steps: 4,
  x_passes: 11,
  x_margin_mm: 3,
  front_overhang_mm: 2,
  back_overhang_mm: 2,
  eject_speed_mm_min: 3000,
  skim_speed_mm_min: 1500,
  cooling_fan_assist: true,
  final_skim: true,
  max_part_height_mm: 42,
  sweep_x_min_mm: null,
  sweep_x_max_mm: null,
  sweep_start_frac: 1,
};

/** Request body for the preview endpoint. */
export interface EjectProfilePreviewRequest {
  library_file_id: number;
  plate_index: number;
}

/** Validation block returned alongside the generated preview G-code. */
export interface EjectProfileValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
}

/** Response from POST /eject-profiles/{id}/preview. */
export interface EjectProfilePreviewResponse {
  gcode: string;
  validation: EjectProfileValidation;
  max_z_height: number;
}

/**
 * Request body for the dry-run dispatch endpoint. Queues a real geometry-only
 * eject test onto a specific connected printer (hardware-ladder step 1: the bed
 * MUST be empty). Mirrors the backend `POST /eject-profiles/{id}/dry-run/dispatch`
 * contract exactly.
 */
export interface EjectProfileDryRunDispatchRequest {
  library_file_id: number;
  plate_index: number;
  printer_id: number;
}

/** Response from POST /eject-profiles/{id}/dry-run/dispatch. */
export interface EjectProfileDryRunDispatchResponse {
  queue_item_id: number;
  library_file_id: number;
  message: string;
}
