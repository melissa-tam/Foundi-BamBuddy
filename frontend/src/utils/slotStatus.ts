/**
 * Pure derivations for the per-AMS-slot status badges on PrintersPage. Kept
 * side-effect-free and dependency-light so the branchy UI logic is unit-testable
 * in isolation. The slot RINGS are not derived here: the backend answers them
 * (`ToolheadState.active_tray` / `was_feeding_tray`).
 */

/** Minimal shape of an HMS error carrying per-slot runout attribution. */
export interface RunoutSlotBearer {
  runout_slot?: { ams_id: number; tray_id: number } | null;
}

/**
 * Whether any live HMS error names THIS AMS slot as the one that ran out. The
 * backend enriches runout-family codes with `runout_slot` {ams_id, tray_id};
 * slot-agnostic runouts (the `_8011`-only case) carry no `runout_slot` and so
 * never light a per-slot badge.
 */
export function slotRanOut(
  hmsErrors: readonly RunoutSlotBearer[] | null | undefined,
  amsId: number,
  trayId: number,
): boolean {
  if (!hmsErrors) return false;
  return hmsErrors.some(
    e => e.runout_slot != null && e.runout_slot.ams_id === amsId && e.runout_slot.tray_id === trayId,
  );
}
