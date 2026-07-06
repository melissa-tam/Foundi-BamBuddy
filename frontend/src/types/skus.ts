/**
 * SKU catalog types (farm production, Phase 2).
 *
 * A SKU (stock-keeping unit) is a sellable part identified by a code. Each SKU
 * links one or more library files (a specific plate of a specific 3MF) that
 * produce a known number of units per plate, plus an optional default eject
 * profile used when a production run of the SKU is dispatched.
 *
 * Field names and units mirror the backend contract exactly — do not rename
 * without updating the backend SKU schema.
 */

/** A file/plate linked to a SKU, with the capability metadata parsed server-side. */
export interface SkuFile {
  id: number;
  sku_id: number;
  library_file_id: number;
  library_file_name: string;
  plate_index: number;
  units_per_plate: number;
  nozzle_diameter: number | null;
  filament_type: string | null;
  printer_model: string | null;
  max_z_height: number | null;
}

/** A persisted SKU as returned by the API. */
export interface Sku {
  id: number;
  code: string;
  name: string;
  part_number: string | null;
  notes: string | null;
  default_eject_profile_id: number | null;
  files: SkuFile[];
  created_at: string;
  updated_at: string;
}

/** Create payload (id/timestamps/files assigned server-side). */
export interface SkuCreate {
  code: string;
  name: string;
  part_number: string | null;
  notes: string | null;
  default_eject_profile_id: number | null;
}

/** Update payload: identical shape to create (full replace via PUT). */
export type SkuUpdate = SkuCreate;

/** Body for POST /skus/{id}/files — link a library-file plate to the SKU. */
export interface SkuFileLinkCreate {
  library_file_id: number;
  plate_index: number;
  units_per_plate: number;
}

/** Response from GET /skus/suggest — parsed hints from a library file's metadata. */
export interface SkuSuggest {
  code: string | null;
  part_number: string | null;
  name: string | null;
  /** Which metadata source the suggestion came from (e.g. filename, model title). */
  matched_from: string | null;
}

/** Lifetime production statistics for a SKU (GET /skus/{id}/stats). */
export interface SkuStats {
  units_completed: number;
  units_failed: number;
  plates_completed: number;
  plates_failed: number;
  /** Fraction 0..1, or null when no plates have completed yet. */
  success_rate: number | null;
  /** Median plate cycle time in seconds, or null when insufficient data. */
  median_cycle_seconds: number | null;
}
