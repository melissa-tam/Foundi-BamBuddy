/**
 * How big a Fleet grid chart draws at each widget size.
 *
 * The `Dashboard` lets an operator cycle any widget between a quarter, a half
 * and the full row, and a plot that kept one height across all three either
 * wastes a full-width card or crushes a quarter-width one. Both ladders live
 * here rather than beside the frame so every widget reads the same numbers —
 * and so a chart's tick size is chosen by its WIDTH, not by each widget's own
 * taste: a 24-column hour axis in a quarter-width card needs smaller labels
 * than the same axis across the row.
 */

/** Plot height in px by grid size (1 = quarter, 2 = half, 4 = full row). */
export const CHART_HEIGHT: Record<1 | 2 | 4, number> = { 1: 170, 2: 210, 4: 280 };

/** Axis tick font size in px by grid size. */
export const AXIS_TICK_SIZE: Record<1 | 2 | 4, number> = { 1: 9, 2: 10, 4: 11 };
