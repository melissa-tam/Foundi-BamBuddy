/**
 * THE tone -> style map of a hold (`HoldChipState.tone`): the hold chip's ground and text,
 * and the touch tile's status edge, which draws its edge in the same `text-*` colour
 * (`border-current`). Its own module so `HoldChip.tsx` stays component-only for the
 * `react-refresh/only-export-components` rule (the `filamentSwatchHelpers.ts` precedent).
 */
import type { HoldChipState } from '../api/client';

export const TONE_CLASS: Record<HoldChipState['tone'], string> = {
  acting: 'bg-yellow-500/20 text-yellow-400',
  held: 'bg-status-error/20 text-status-error',
};
