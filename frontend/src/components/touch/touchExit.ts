/**
 * Leaving the touch view: its Exit navigates to `/` carrying this state, and `Layout`
 * reads it to skip the one-time redirect to the default view — otherwise a kiosk whose
 * default view IS the touch view would bounce straight back.
 */
export const TOUCH_EXIT_STATE = { touchExit: true } as const;

/** Whether a router location state is the touch view's Exit. */
export function isTouchExit(state: unknown): boolean {
  return typeof state === 'object' && state !== null && (state as { touchExit?: unknown }).touchExit === true;
}
