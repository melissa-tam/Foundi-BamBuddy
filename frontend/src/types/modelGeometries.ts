/**
 * Printer model-geometry registry types (farm eject, Phase 2).
 *
 * The backend owns the geometry registry (bed size, travel envelope, part
 * height ceiling, hardware-validation flag per printer model); the frontend
 * reads it to drive eject-profile validation bounds, the model picker on the
 * preview/dry-run tools, and the "eject not qualified" hint on the printer
 * model dropdown.
 *
 * Field names mirror the backend contract exactly — do not rename without
 * updating `backend/app/schemas/model_geometry.py`.
 */

/** One registry row as returned by GET /model-geometry. */
export interface ModelGeometry {
  /** Canonical model key (e.g. "H2S", "H2C"). */
  model_key: string;
  /** Bed width (mm, X). */
  bed_x: number;
  /** Bed depth (mm, Y). */
  bed_y: number;
  /** Travel envelope X minimum (mm). */
  env_x_min: number;
  /** Travel envelope X maximum (mm). */
  env_x_max: number;
  /** Travel envelope Y minimum (mm; may be negative — overhang past the bed). */
  env_y_min: number;
  /** Travel envelope Y maximum (mm). */
  env_y_max: number;
  /** Tallest part (mm) the eject sweep is rated for on this model. */
  max_part_height_mm: number;
  /** Commandable Z ceiling (mm) — the machine's maximum bed-drop travel. Drives
   *  the eject bed-drop release assist; null ⇒ bed-drop generation fails closed.
   *  (Backend column `z_travel_mm`.) */
  z_travel_mm: number | null;
  /** True only after the hardware ladder was operator-witnessed on this model.
   *  Production eject dispatch is blocked while false. */
  validated: boolean;
  /** Derived (read-only): true when this model is a bed-slinger — the bed is
   *  FIXED in Z and the gantry carries the Z axis, so the bed-drop release
   *  assist is physically meaningless and unavailable on it. Computed by the
   *  backend from `model_key`; NOT a column and NOT part of ModelGeometryUpdate. */
  bedslinger: boolean;
  notes: string | null;
  updated_at: string;
}

/** GET /model-geometry envelope: rows + the server's minimum sweep-band width
 *  constant, so the client never hardcodes a copy of it. */
export interface ModelGeometryList {
  geometries: ModelGeometry[];
  sweep_band_min_width_mm: number;
}

/** PUT /model-geometry/{model_key} body — every field optional. */
export interface ModelGeometryUpdate {
  bed_x?: number;
  bed_y?: number;
  env_x_min?: number;
  env_x_max?: number;
  env_y_min?: number;
  env_y_max?: number;
  max_part_height_mm?: number;
  z_travel_mm?: number | null;
  validated?: boolean;
  notes?: string | null;
}

/**
 * Client-side mirror of the backend's model-key canonicalisation, sufficient
 * for matching UI dropdown values ("H2S", "H2D Pro", …) against the registry's
 * canonical keys: uppercase with whitespace removed. Slice-code aliases
 * (O1S -> H2S) are resolved server-side only.
 */
export function canonModelKey(model: string | null | undefined): string | null {
  if (!model) return null;
  const key = model.toUpperCase().replace(/\s+/g, '');
  return key.length > 0 ? key : null;
}

/** Find the registry row for a (display) model name, or null when unknown. */
export function findGeometry(
  geometries: ModelGeometry[] | undefined,
  model: string | null | undefined,
): ModelGeometry | null {
  const key = canonModelKey(model);
  if (!key || !geometries) return null;
  return geometries.find((g) => canonModelKey(g.model_key) === key) ?? null;
}
