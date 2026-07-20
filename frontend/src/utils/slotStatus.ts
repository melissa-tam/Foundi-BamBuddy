/**
 * Pure derivations for the per-AMS-slot status badges/rings on PrintersPage (W6).
 * Kept side-effect-free and dependency-light so the branchy UI logic is unit-
 * testable in isolation (the render sites just call these and compare ids).
 */

/** Minimal shape of an HMS error carrying per-slot runout attribution. */
export interface RunoutSlotBearer {
  runout_slot?: { ams_id: number; tray_id: number } | null;
}

/**
 * The tray that WAS feeding this job while the active green ring has cleared —
 * i.e. during a runout PAUSE where `tray_now` flips to 255 (so `effectiveTrayNow`
 * is undefined) exactly when the operator needs to know which slot to refill.
 *
 * Returns the global tray id to dim-ring, or `undefined` when no was-feeding hint
 * applies: a job must be active (RUNNING/PAUSE), there must be no live active
 * tray, and `last_loaded_tray` must name a real tray (>= 0; -1 = none).
 */
export function wasFeedingTrayId(
  state: string | null | undefined,
  effectiveTrayNow: number | undefined,
  lastLoadedTray: number | null | undefined,
): number | undefined {
  const jobActive = state === 'RUNNING' || state === 'PAUSE';
  if (
    jobActive &&
    effectiveTrayNow === undefined &&
    lastLoadedTray != null &&
    lastLoadedTray >= 0
  ) {
    return lastLoadedTray;
  }
  return undefined;
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
