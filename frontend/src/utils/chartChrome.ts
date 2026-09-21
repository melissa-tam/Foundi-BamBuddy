/**
 * Recharts chrome — THE owner of how every chart's furniture is coloured.
 *
 * "Chrome" is everything around the data: the tooltip panel, the axis lines and
 * their tick labels, the grid, the legend text. It is NOT the data encoding —
 * series colours, heat ramps and class palettes belong to the module that owns
 * the data being drawn.
 *
 * Why one origin: a chart's furniture is not a per-chart decision. A tooltip
 * panel re-typed per chart drifts (background, radius, axis grey), and the
 * source pins in `chartChrome.test.ts` fail on a chart that defines its own.
 *
 * THEME-DERIVED. The app ships 8 grounds (light neutral/warm/cool and dark
 * neutral/warm/cool/oled/slate) plus 6 accents, so a hardcoded grey is only
 * ever right on one of them — a dark-neutral literal is unreadable in light
 * mode. Every value here is a `var(--…)` from `index.css`. Recharts accepts a CSS
 * colour string in both places it needs one:
 *   - `contentStyle` / `labelStyle` / `itemStyle` land on a real DOM node's
 *     inline style, where `var()` has always worked;
 *   - `stroke` / `fill` land on SVG presentation attributes, where `var()`
 *     resolves too (verified in Chromium: `stroke="var(--border-color)"`
 *     computes to the same rgb as the literal). `AMSHistoryModal` and
 *     `HeaterHistoryModal` have shipped that form since before this module.
 *
 * NO ACCENT COLOUR. `--accent` is user-selectable (red is one of the choices),
 * so chrome never derives from it — chrome must not change meaning when the
 * operator changes theme.
 */
import type { CSSProperties } from 'react';

/**
 * Secondary chrome text: tick labels, legend entries, the annotation half of a
 * tooltip row. Exported as a bare string for the places that need the colour
 * on its own rather than in a style object.
 *
 * `--text-secondary`, not `--text-muted`: muted fails WCAG AA as body text on
 * several grounds (`#808080` on `#2d2d2d` is ~3.6:1), and every one of these is
 * text a reader has to parse.
 */
export const CHART_MUTED_TEXT = 'var(--text-secondary)';

/**
 * Axis line AND tick-label colour. Recharts derives the tick text `fill` from
 * the axis `stroke`, so one value covers both; `chartAxisTick` sets the fill
 * explicitly anyway so the text colour does not depend on that internal.
 */
export const CHART_AXIS_STROKE = CHART_MUTED_TEXT;

/** Grid lines. Same hairline as every card border in the app. */
export const CHART_GRID_STROKE = 'var(--border-color)';

/**
 * The hairline between two segments of ONE stacked bar.
 *
 * The card's own ground, so the separator reads as a gap rather than as a
 * fourth colour — and it is a `var()`, so it is the right gap on all 8 grounds.
 *
 * Why it exists: a stacked bar encodes its segments by fill, and two adjacent
 * fills that a colour-blind reader cannot distinguish become one indivisible
 * block — the segments stop being COUNTABLE. A 1 px ground-coloured edge makes
 * the boundaries legible whatever happens to the hues, which is the cheapest
 * redundant channel a stack can carry and the only one that costs no legend.
 */
export const CHART_BAND_SEPARATOR_STROKE = 'var(--bg-secondary)';

/** Width of that hairline. One CSS pixel — a separator, never a border. */
export const CHART_BAND_SEPARATOR_WIDTH = 1;

/** The dash pattern every chart in the app already used for its grid. */
export const CHART_GRID_DASH = '3 3';

/** Tick label size where a chart has no reason to pick another. */
export const CHART_TICK_FONT_SIZE = 11;

interface ChartAxisTick {
  fill: string;
  fontSize: number;
}

/**
 * Tick label style for an `XAxis`/`YAxis` `tick` prop. Size is a per-chart
 * density decision (a 24-column hour axis needs smaller labels than a 7-column
 * one), so it is the one parameter; the colour is not negotiable.
 */
export function chartAxisTick(fontSize: number = CHART_TICK_FONT_SIZE): Readonly<ChartAxisTick> {
  return Object.freeze({ fill: CHART_AXIS_STROKE, fontSize });
}

/**
 * The tooltip panel. Spread it when a chart needs a denser font:
 * `contentStyle={{ ...CHART_TOOLTIP_CONTENT_STYLE, fontSize: 11 }}`.
 */
export const CHART_TOOLTIP_CONTENT_STYLE: Readonly<CSSProperties> = Object.freeze({
  backgroundColor: 'var(--bg-secondary)',
  border: '1px solid var(--border-color)',
  borderRadius: '8px',
  color: 'var(--text-primary)',
});

/** The tooltip's heading row (the x-axis category). */
export const CHART_TOOLTIP_LABEL_STYLE: Readonly<CSSProperties> = Object.freeze({
  color: 'var(--text-primary)',
});

/**
 * Tooltip body text that is NOT the series value — units, annotations, the
 * secondary half of a hand-rolled tooltip row. Recharts' own tooltip colours
 * each item by its series, so the built-in `itemStyle` is deliberately left
 * unset; this is for custom `content` renderers.
 */
export const CHART_TOOLTIP_ITEM_STYLE: Readonly<CSSProperties> = Object.freeze({
  color: CHART_MUTED_TEXT,
});

/** Legend entry text where a chart overrides recharts' series-coloured default. */
export const CHART_LEGEND_TEXT_STYLE: Readonly<CSSProperties> = Object.freeze({
  color: CHART_MUTED_TEXT,
});
