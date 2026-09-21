/**
 * Fleet class → label, colour, pattern, heat ramp, band, absence, format.
 *
 * THE encoding owner for the Fleet tab. Every literal the tab draws or reads a
 * class key with lives here; a component that spells a class key, a hex or a
 * threshold of its own has a second definition of the same fact, and the two
 * drift on the day the backend adds a cause.
 *
 * Deliberately NOT here: generic recharts furniture (`utils/chartChrome.ts` —
 * it serves the Prints tab too) and preset → date range (`utils/timeframe.ts`).
 *
 * ## Why the palette looks the way it does
 *
 * The app ships **8 grounds** (light neutral/warm/cool, dark neutral/warm/cool/
 * oled/slate) and **6 user-selectable accents, red among them**. Two
 * consequences the whole palette is built around:
 *
 * 1. **No accent colour may encode data.** `--accent` / `bambu-green` change
 *    meaning when the operator changes theme, so a band coloured from one says
 *    something different on Tuesday. Every data colour here is a literal, and
 *    `__tests__/utils/fleetMetrics.test.ts` fails on an accent token appearing.
 * 2. **Heat tiles are opaque, never alpha over the ground.** A 30 %-alpha red
 *    has nine different contrasts against nine grounds, so its text can only
 *    be checked on one of them. Each heat step is a fixed hex PAIRED with the
 *    text colour that passes WCAG 4.5:1 on it, and the test computes the ratio
 *    rather than trusting the eye.
 * 3. **A heat ramp has TWO directions, chosen by the resolved theme mode.**
 *    Opaque and theme-invariant are not the same thing. A single pale→saturated
 *    ramp is right on a light ground and exactly backwards on a dark one: the
 *    palest tile is then the BRIGHTEST thing on the page, so "under 1 h down"
 *    shouts and "12 h down" recedes. Six of this app's nine grounds are dark
 *    and dark is the default, so the light-only ramp would have been wrong most
 *    of the time. `lensRamp(lens, mode)` therefore takes the RESOLVED mode
 *    (`useTheme().resolvedMode` — the app's one resolver, never re-resolved
 *    here) and emphasis rises with the value on both: luminance falls with the
 *    step on light, rises on dark. Both ramps end on the same saturated cap.
 *
 * Hue comes from the theme-INVARIANT status tokens in `index.css`
 * (`--status-ok` `#22c55e`, `--status-error` `#ef4444`, `--status-warning`
 * `#f59e0b`) — those three are defined once and never re-declared per theme.
 * Because a jsdom test cannot measure a `var()`, the ladder is written as the
 * literal at each band's VALUE step and pinned two ways: the hue stays in its
 * token's family, and `index.css` still declares the token it was derived from.
 *
 * Three redundant channels carry every class, because one is never enough:
 * **hue** (which class), **value** (a strictly monotonic luminance ladder, so
 * the bands separate in greyscale and under deuteranopia) and **pattern**
 * (down = 45° hatch, maintenance = dots, no data = sparse hatch).
 */

import type { ResolvedThemeMode } from '../contexts/ThemeContext';
// Re-exported so a consumer's signature can name the mode without importing
// React context types; ThemeContext remains the one place it is RESOLVED.
export type { ResolvedThemeMode };
import type {
  FleetBasis,
  FleetBucket,
  FleetCause,
  FleetClassKey,
  FleetConditionCause,
  FleetCycleCause,
  FleetFaultKind,
  FleetGroup,
  MatrixCell,
  MatrixPrinter,
  PrintOutcome,
  SeriesBucket,
  SeriesEnvelope,
  SummaryRowKey,
} from '../types/fleetMetrics';

// ── class keys ──────────────────────────────────────────────────────────────

/** The prefix an equipment fault names its incident kind after. */
const FAULT_PREFIX = 'fault:';

const GROUPS: readonly FleetGroup[] = [
  'printing',
  'cycle_overhead',
  'idle',
  'down',
  'planned',
  'out_of_fleet',
  'not_recorded',
  'unobserved',
];

const GROUP_SET: ReadonlySet<string> = new Set(GROUPS);

const CYCLE_CAUSES: readonly FleetCycleCause[] = ['cooling', 'ejecting'];

const CONDITION_CAUSES: readonly FleetConditionCause[] = [
  'offline',
  'paused',
  'quarantined',
  'plate_held',
  'model_mismatch',
  'no_usb',
];

/**
 * Fault kinds in the store's own precedence order
 * (`models/printer_incident.KIND_PRECEDENCE` less the declared `service_hold`):
 * the kinds that interrupt a RUNNING print come first.
 */
export const FAULT_KIND_ORDER: readonly FleetFaultKind[] = [
  'physical',
  'runout',
  'jam',
  'power_loss',
  'plate_vision',
  'z_reference_lost',
];

/** The five groups a stacked chart draws, BOTTOM to TOP. */
export const CHART_STACK_GROUPS = [
  'printing',
  'cycle_overhead',
  'idle',
  'planned',
  'down',
] as const satisfies readonly FleetGroup[];

/** A group that is actually drawn as a band. The three absence groups are not. */
export type ChartedGroup = (typeof CHART_STACK_GROUPS)[number];

/**
 * Down causes, ACTIONABLE FIRST.
 *
 * A plate nobody cleared, a missing USB drive, a pause and a model mismatch are
 * all fixed in under a minute by somebody who is already standing there; an
 * offline printer, a quarantine and an equipment fault are not. The legend and
 * the cause-split chart read in this order so the first band a reader sees is
 * the one they can do something about.
 */
export const DOWN_CAUSE_ORDER: readonly FleetCause[] = [
  'plate_held',
  'no_usb',
  'paused',
  'model_mismatch',
  'offline',
  'quarantined',
  ...FAULT_KIND_ORDER.map((kind): FleetCause => `${FAULT_PREFIX}${kind}` as FleetCause),
];

/** What one class key means, split once. */
export interface ParsedClassKey {
  group: FleetGroup;
  cause: FleetCause | null;
  /** The incident kind behind a `down:fault:*` key; null for every other class. */
  faultKind: FleetFaultKind | null;
}

/**
 * Split a class key — THE only place a class key is taken apart.
 *
 * Strict on the GROUP: an unrecognised group is a contract break between this
 * build and the backend's classifier, and failing loudly beats a silently
 * missing chart band. Permissive on the fault KIND, because a newly registered
 * incident kind reaches the surface through the same dynamic lookup the printer
 * card's chip already uses (`printers.incident.<kind>`, pinned by
 * `__tests__/i18n/incidentKinds.test.ts`).
 */
export function parseClassKey(key: string): ParsedClassKey {
  const separator = key.indexOf(':');
  const group = separator === -1 ? key : key.slice(0, separator);
  if (!GROUP_SET.has(group)) {
    throw new RangeError(`fleetMetrics: unknown class group in "${key}"`);
  }
  if (separator === -1) {
    return { group: group as FleetGroup, cause: null, faultKind: null };
  }
  const cause = key.slice(separator + 1);
  if (cause.startsWith(FAULT_PREFIX)) {
    const kind = cause.slice(FAULT_PREFIX.length);
    if (!kind) throw new RangeError(`fleetMetrics: empty fault kind in "${key}"`);
    return {
      group: group as FleetGroup,
      cause: cause as FleetCause,
      faultKind: kind as FleetFaultKind,
    };
  }
  return { group: group as FleetGroup, cause: cause as FleetCause, faultKind: null };
}

/** Every class key the classifier can emit, in a stable order. For tests and legends. */
export function allClassKeys(): FleetClassKey[] {
  return [
    'printing',
    ...CYCLE_CAUSES.map((cause): FleetClassKey => `cycle_overhead:${cause}`),
    'idle',
    ...CONDITION_CAUSES.map((cause): FleetClassKey => `down:${cause}`),
    ...FAULT_KIND_ORDER.map((kind): FleetClassKey => `down:fault:${kind}`),
    'planned',
    'out_of_fleet',
    'not_recorded',
    'unobserved',
  ];
}

// ── labels ──────────────────────────────────────────────────────────────────
//
// Keys, never strings: the module never calls `t()`, so it stays pure and the
// components decide when copy is resolved. Existing keys are REUSED wherever
// the concept is the same — "Offline" on the Fleet tab and "Offline" on the
// printer card are one fact, and a second leaf for it is a second thing to
// translate eleven times and a second thing to get out of step.

const GROUP_LABEL_KEY: Record<FleetGroup, string> = {
  printing: 'printers.status.printing',
  cycle_overhead: 'fleetMetrics.class.cycle_overhead',
  idle: 'printers.status.idle',
  down: 'fleetMetrics.class.down',
  planned: 'fleetMetrics.class.planned',
  out_of_fleet: 'fleetMetrics.class.out_of_fleet',
  not_recorded: 'fleetMetrics.class.not_recorded',
  unobserved: 'fleetMetrics.class.unobserved',
};

const CAUSE_LABEL_KEY: Record<FleetCycleCause | FleetConditionCause, string> = {
  cooling: 'fleetMetrics.class.cooling',
  ejecting: 'fleetMetrics.class.ejecting',
  offline: 'printers.status.offline',
  paused: 'printers.status.paused',
  quarantined: 'printers.quarantine.badge',
  plate_held: 'productionRuns.detail.eligibility.awaitingPlateClear',
  model_mismatch: 'printers.modelMismatch.badge',
  no_usb: 'printers.noUsb',
};

/** The i18n key for a group's own label. */
export function groupLabelKey(group: FleetGroup): string {
  return GROUP_LABEL_KEY[group];
}

/** The i18n key for a cause's label, fault kinds included. */
export function causeLabelKey(cause: FleetCause): string {
  if (cause.startsWith(FAULT_PREFIX)) {
    return `printers.incident.${cause.slice(FAULT_PREFIX.length)}`;
  }
  return CAUSE_LABEL_KEY[cause as FleetCycleCause | FleetConditionCause];
}

/** The i18n key for a whole class key — its cause's label, or its group's. */
export function classLabelKey(key: string): string {
  const { group, cause } = parseClassKey(key);
  return cause === null ? groupLabelKey(group) : causeLabelKey(cause);
}

/**
 * The one-sentence `InfoHint` for a group, or null where the label is its own
 * explanation. Supplementary copy rides a tooltip, never the primary surface
 * (react-best-practices §9).
 */
export function groupHintKey(group: FleetGroup): string | null {
  switch (group) {
    case 'down':
      return 'fleetMetrics.hints.down';
    case 'cycle_overhead':
      return 'fleetMetrics.hints.cycle_overhead';
    case 'planned':
      return 'fleetMetrics.hints.planned';
    case 'unobserved':
      return 'fleetMetrics.hints.unobserved';
    case 'not_recorded':
      return 'fleetMetrics.hints.not_recorded';
    default:
      return null;
  }
}

/** Summary row key → its label leaf. The backend composes the rows; this names them. */
export const SUMMARY_ROW_LABEL_KEY: Record<SummaryRowKey, string> = {
  avg_printing: 'fleetMetrics.summary.rows.avg_printing',
  avg_cycle_overhead: 'fleetMetrics.summary.rows.avg_cycle_overhead',
  avg_idle: 'fleetMetrics.summary.rows.avg_idle',
  avg_down: 'fleetMetrics.summary.rows.avg_down',
  avg_planned: 'fleetMetrics.summary.rows.avg_planned',
  peak_down: 'fleetMetrics.summary.rows.peak_down',
  printers_in_fleet: 'fleetMetrics.summary.rows.printers_in_fleet',
  prints_per_day: 'fleetMetrics.summary.rows.prints_per_day',
  prints_per_printer_per_day: 'fleetMetrics.summary.rows.prints_per_printer_per_day',
  uptime: 'fleetMetrics.summary.rows.uptime',
  time_printing: 'fleetMetrics.summary.rows.time_printing',
};

/** The rows that carry an `InfoHint`. Every other row's label says it all. */
export const SUMMARY_ROW_HINT_KEY: Partial<Record<SummaryRowKey, string>> = {
  avg_cycle_overhead: 'fleetMetrics.hints.cycle_overhead',
  avg_down: 'fleetMetrics.hints.down',
  avg_planned: 'fleetMetrics.hints.planned',
  prints_per_printer_per_day: 'fleetMetrics.hints.prints_per_printer',
  uptime: 'fleetMetrics.hints.uptime',
  time_printing: 'fleetMetrics.hints.time_printing',
};

/** How each row's figure is formatted — the summary card's one switch. */
export const SUMMARY_ROW_FORMAT: Record<SummaryRowKey, 'printers' | 'count' | 'ratio'> = {
  avg_printing: 'printers',
  avg_cycle_overhead: 'printers',
  avg_idle: 'printers',
  avg_down: 'printers',
  avg_planned: 'printers',
  peak_down: 'count',
  printers_in_fleet: 'printers',
  prints_per_day: 'count',
  prints_per_printer_per_day: 'printers',
  uptime: 'ratio',
  time_printing: 'ratio',
};

/** Print outcome → its label leaf. */
export const OUTCOME_LABEL_KEY: Record<PrintOutcome, string> = {
  completed: 'fleetMetrics.detail.outcome.completed',
  failed: 'fleetMetrics.detail.outcome.failed',
  cancelled: 'fleetMetrics.detail.outcome.cancelled',
  other: 'fleetMetrics.detail.outcome.other',
};

/** Outcome bands in the order the Prints-per-day chart stacks them. */
export const OUTCOME_ORDER: readonly PrintOutcome[] = ['completed', 'failed', 'cancelled', 'other'];

/** The matrix's three lenses. */
export type FleetLens = 'prints' | 'hours_down' | 'time_split';

export const FLEET_LENSES: readonly FleetLens[] = ['prints', 'hours_down', 'time_split'];

export const LENS_LABEL_KEY: Record<FleetLens, string> = {
  prints: 'fleetMetrics.matrix.lens.prints',
  hours_down: 'fleetMetrics.matrix.lens.hoursDown',
  time_split: 'fleetMetrics.matrix.lens.timeSplit',
};

/** `basis` → the label a hatched cell's tooltip names it with. */
export const BASIS_LABEL_KEY: Record<FleetBasis, string> = {
  observed: 'fleetMetrics.class.unobserved',
  incidents_only: 'fleetMetrics.class.incidents_only',
};

// ── colour ──────────────────────────────────────────────────────────────────

/**
 * The theme-invariant status tokens this palette's hues derive from, as the
 * literals `src/index.css` declares. Pinned there by the test: a token
 * re-valued in CSS without this constant following it would silently move every
 * band off its hue family.
 */
export const FLEET_STATUS_TOKEN_HEX = {
  ok: '#22c55e',
  error: '#ef4444',
  warning: '#f59e0b',
} as const;

/**
 * The value ladder. Strictly DESCENDING in relative luminance —
 * printing 0.553 › between prints 0.363 › maintenance 0.280 › down 0.229 ›
 * idle 0.167 — so the five bands separate in greyscale and under deuteranopia
 * even before hue and pattern are read.
 *
 * Hue families: printing green (`--status-ok`), between prints blue-400, down
 * red (the `--status-error` literal itself), maintenance amber
 * (`--status-warning`), idle a true neutral (saturation 0).
 */
export const FLEET_GROUP_COLOR: Record<ChartedGroup, string> = {
  printing: '#4ade80',
  cycle_overhead: '#60a5fa',
  idle: '#737373',
  planned: '#d97706',
  down: '#ef4444',
};

/** The ladder, brightest first — the order the luminance test walks. */
export const VALUE_LADDER: readonly ChartedGroup[] = [
  'printing',
  'cycle_overhead',
  'planned',
  'down',
  'idle',
];

/** Text that is legible on any light tile in this palette. */
export const HEAT_TEXT_DARK = '#14161a';
/** Text for the saturated and deep ends of a ramp. */
export const HEAT_TEXT_LIGHT = '#ffffff';

/**
 * The text colour PROVEN legible on each band colour when it is used as a
 * TEXT-BEARING tile (a chip, a data-table swatch with a figure in it).
 *
 * Measured, not guessed, and the result is not uniform: idle `#737373` is the
 * one band where white wins (4.74 against dark text's 3.82), and `#ef4444`
 * goes the other way (dark 4.81 against white's 3.76). As plain chart-band
 * FILLS, with no text on them, these need no pairing at all — the pairing
 * exists so that a later widget putting a number inside a swatch has an answer
 * it does not have to invent.
 */
export const FLEET_GROUP_TEXT: Record<ChartedGroup, string> = {
  printing: HEAT_TEXT_DARK,
  cycle_overhead: HEAT_TEXT_DARK,
  idle: HEAT_TEXT_LIGHT,
  planned: HEAT_TEXT_DARK,
  down: HEAT_TEXT_DARK,
};

/**
 * The three groups that are an ABSENCE of data rather than a state.
 * `unobserved` gets a neutral swatch so a legend can show its sparse hatch;
 * the other two are never a fill (a dash and a flat ground respectively).
 */
export const FLEET_ABSENCE_COLOR: Record<'unobserved', string> = {
  unobserved: '#9ca3af',
};

/** Text proven legible on the `unobserved` swatch (7.13:1). */
export const FLEET_ABSENCE_TEXT: Record<'unobserved', string> = {
  unobserved: HEAT_TEXT_DARK,
};

/**
 * The Time-split lens's three bands. Categorical, not a magnitude scale — the
 * bar is always printing | other | down in that order with a legend — so
 * emphasis does not need to track a value and one set serves both modes. Every
 * band clears 1.25:1 against all nine grounds (pinned in the test).
 */
export const TIME_SPLIT_BAND_COLOR: Record<'printing' | 'other' | 'down', string> = {
  printing: FLEET_GROUP_COLOR.printing,
  other: FLEET_GROUP_COLOR.idle,
  down: FLEET_GROUP_COLOR.down,
};

/** Text proven legible on each Time-split band, for an in-bar percentage. */
export const TIME_SPLIT_BAND_TEXT: Record<'printing' | 'other' | 'down', string> = {
  printing: FLEET_GROUP_TEXT.printing,
  other: FLEET_GROUP_TEXT.idle,
  down: FLEET_GROUP_TEXT.down,
};

/**
 * Down causes as a 12-step shade ladder inside the down hue.
 *
 * All twelve are the same fact — the printer cannot take work — so they share
 * red and separate by VALUE, in the actionable-first order. Twelve distinct
 * hues would say the causes are twelve different kinds of thing, and would have
 * to come from somewhere; the only other hues in this palette already mean
 * printing, between prints and maintenance.
 *
 * The ladder deliberately JUMPS between step 5 (`#d45c5c`, L 0.224) and step 6
 * (`#ce4646`, L 0.179). Between those luminances lies a dead band where a red
 * tile carries neither white text (< 4.5:1) nor dark text (< 4.5:1); an evenly
 * spaced ramp put a shade in it, and that shade could not legibly carry a
 * figure. Steps 0–5 take dark text, 6–11 take white.
 */
const DOWN_CAUSE_SHADES: readonly string[] = [
  '#e8adad',
  '#e59c9c',
  '#e08c8c',
  '#dc7a7a',
  '#d76a6a',
  '#d45c5c',
  '#ce4646',
  '#c73535',
  '#b53030',
  '#a42b2b',
  '#912727',
  '#7d2121',
];

/** Where the shade ladder crosses from dark text to white text. */
const DOWN_CAUSE_WHITE_TEXT_FROM = 6;

const lastShade = DOWN_CAUSE_SHADES[DOWN_CAUSE_SHADES.length - 1] ?? '#7d2121';

/** The band colour for one down cause. Unknown causes take the darkest shade. */
export function downCauseColor(cause: FleetCause): string {
  const index = DOWN_CAUSE_ORDER.indexOf(cause);
  if (index === -1) return lastShade;
  return DOWN_CAUSE_SHADES[index] ?? lastShade;
}

/** The text colour proven legible on that cause's band. */
export function downCauseTextColor(cause: FleetCause): string {
  const index = DOWN_CAUSE_ORDER.indexOf(cause);
  return index === -1 || index >= DOWN_CAUSE_WHITE_TEXT_FROM ? HEAT_TEXT_LIGHT : HEAT_TEXT_DARK;
}

/** One heat step: an opaque tile and the text colour proven legible on it. */
export interface HeatStep {
  background: string;
  text: string;
}

/** Which of the five steps a value falls in. Index 0 always means "nothing". */
export type HeatIndex = 0 | 1 | 2 | 3 | 4;

/**
 * LIGHT ground, hours down: palest → capped red, so emphasis rises as the tile
 * darkens against a pale page. Measured contrasts against the paired text:
 * 15.64 · 11.65 · 7.88 · 5.29 · 4.96. Capped at `#c74040` precisely so white
 * still clears 4.5:1 on the hottest step.
 *
 * Step 0 is the legend's low end. A genuinely-zero cell never paints a tile —
 * `cellAbsence` answers `zero` first and the matrix renders the dim `0` — so
 * the extreme step reaches a cell only through the legend swatch beside it.
 */
export const HEAT_RAMP_HOURS_DOWN_LIGHT: readonly HeatStep[] = [
  { background: '#fdeaea', text: HEAT_TEXT_DARK },
  { background: '#f6c3c3', text: HEAT_TEXT_DARK },
  { background: '#e79595', text: HEAT_TEXT_DARK },
  { background: '#d66a6a', text: HEAT_TEXT_DARK },
  { background: '#c74040', text: HEAT_TEXT_LIGHT },
];

/**
 * DARK ground, hours down: deep muted → the SAME capped red, luminance rising
 * with the step so the hottest tile is again the loudest thing in the column.
 * Measured against white text: 13.33 · 9.85 · 7.69 · 6.10 · 4.96.
 */
export const HEAT_RAMP_HOURS_DOWN_DARK: readonly HeatStep[] = [
  { background: '#5f1111', text: HEAT_TEXT_LIGHT },
  { background: '#851717', text: HEAT_TEXT_LIGHT },
  { background: '#a21d1d', text: HEAT_TEXT_LIGHT },
  { background: '#be2222', text: HEAT_TEXT_LIGHT },
  { background: '#c74040', text: HEAT_TEXT_LIGHT },
];

/**
 * LIGHT ground, prints: palest → capped green. Measured: 16.13 · 13.36 ·
 * 10.14 · 6.20 · 5.41.
 */
export const HEAT_RAMP_PRINTS_LIGHT: readonly HeatStep[] = [
  { background: '#e8f5ec', text: HEAT_TEXT_DARK },
  { background: '#c2e6cd', text: HEAT_TEXT_DARK },
  { background: '#8fd0a8', text: HEAT_TEXT_DARK },
  { background: '#4da878', text: HEAT_TEXT_DARK },
  { background: '#187a3a', text: HEAT_TEXT_LIGHT },
];

/**
 * DARK ground, prints: deep muted → the same `#187a3a` cap, rising. Measured
 * against white text: 13.01 · 9.84 · 7.89 · 6.52 · 5.41.
 */
export const HEAT_RAMP_PRINTS_DARK: readonly HeatStep[] = [
  { background: '#0a391b', text: HEAT_TEXT_LIGHT },
  { background: '#0e4e24', text: HEAT_TEXT_LIGHT },
  { background: '#115e2c', text: HEAT_TEXT_LIGHT },
  { background: '#136c32', text: HEAT_TEXT_LIGHT },
  { background: '#187a3a', text: HEAT_TEXT_LIGHT },
];

/** A lens that heats its cells. `time_split` paints bands, not heat. */
export type HeatLens = Exclude<FleetLens, 'time_split'>;

const RAMPS: Record<HeatLens, Record<ResolvedThemeMode, readonly HeatStep[]>> = {
  hours_down: { light: HEAT_RAMP_HOURS_DOWN_LIGHT, dark: HEAT_RAMP_HOURS_DOWN_DARK },
  prints: { light: HEAT_RAMP_PRINTS_LIGHT, dark: HEAT_RAMP_PRINTS_DARK },
};

/**
 * The ramp a lens heats its cells with, for the RESOLVED theme mode.
 *
 * `mode` is required and is a parameter, not a lookup: this module is pure and
 * React-free, and the app already resolves `system` in exactly one place
 * (`ThemeContext.resolvedMode`). A caller passes that value down; nothing here
 * ever re-resolves it, because a second resolver is how a page ends up drawing
 * a light ramp on a dark ground for one render.
 */
export function lensRamp(lens: HeatLens, mode: ResolvedThemeMode): readonly HeatStep[] {
  return RAMPS[lens][mode];
}

/**
 * The class for secondary label text.
 *
 * `bambu-gray` maps to `--text-muted`, which is `#808080` on the dark grounds —
 * about 3.6:1 on `#2d2d2d`, under AA for body text. `bambu-gray-light` maps to
 * `--text-secondary` and clears it on every ground.
 */
export const SECONDARY_TEXT_CLASS = 'text-bambu-gray-light';

// ── patterns ────────────────────────────────────────────────────────────────

/** The three patterns. Redundant channel three: readable with no colour at all. */
export type FleetPattern = 'hatch' | 'dots' | 'sparse';

/**
 * Stable SVG `<pattern>` ids, defined once by `components/fleet/FleetPatternDefs`.
 * Referenced from recharts as `fill={patternFill('hatch')}`.
 */
export const FLEET_PATTERN_IDS: Record<FleetPattern, string> = {
  hatch: 'fleet-pattern-hatch',
  dots: 'fleet-pattern-dots',
  sparse: 'fleet-pattern-sparse',
};

/** The `fill` / `stroke` value that references one pattern. */
export function patternFill(pattern: FleetPattern): string {
  return `url(#${FLEET_PATTERN_IDS[pattern]})`;
}

/** A CSS background pair — an HTML table cell needs no SVG to carry a pattern. */
export interface PatternCss {
  backgroundImage: string;
  backgroundSize: string;
}

/**
 * The CSS equivalents of the three `<pattern>` defs, for HTML surfaces (matrix
 * cells, legend swatches).
 *
 * `currentColor` on purpose: on an opaque heat tile the cell already sets its
 * text colour to the step's paired colour, so the pattern inherits a stroke
 * that is proven legible on that exact tile. The SVG defs cannot do the same —
 * a paint server resolves `currentColor` inconsistently across engines — so
 * they use `var(--text-primary)`, which is right for the chart ground they are
 * drawn on.
 */
export const FLEET_PATTERN_CSS: Record<FleetPattern, PatternCss> = {
  hatch: {
    backgroundImage:
      'repeating-linear-gradient(45deg, currentColor 0, currentColor 2px, transparent 2px, transparent 6px)',
    backgroundSize: 'auto',
  },
  dots: {
    backgroundImage: 'radial-gradient(currentColor 1px, transparent 1.2px)',
    backgroundSize: '6px 6px',
  },
  sparse: {
    backgroundImage:
      'repeating-linear-gradient(45deg, currentColor 0, currentColor 2px, transparent 2px, transparent 12px)',
    backgroundSize: 'auto',
  },
};

/** The pattern a group carries wherever it is drawn. Groups not listed carry none. */
export const GROUP_PATTERN: Partial<Record<FleetGroup, FleetPattern>> = {
  down: 'hatch',
  planned: 'dots',
  unobserved: 'sparse',
};

// ── bands ───────────────────────────────────────────────────────────────────

/** How many hours wide each bucket is. */
export const BUCKET_HOURS: Record<FleetBucket, number> = { hour: 1, day: 24, week: 168 };

/**
 * Column width in px by the server's ECHOED bucket. Deterministic, so the
 * column layout is testable in jsdom: hour is heat-only (the detail opens from
 * a row control instead of a 14 px cell — WCAG 2.5.8), day fits a number, week
 * fits a wider one.
 */
export const BUCKET_COLUMN_WIDTH_PX: Record<FleetBucket, number> = { hour: 14, day: 28, week: 40 };

/**
 * localStorage key of the Fleet tab's widget grid. Here rather than beside the
 * grid because the Stats page header also reads it: "Reset layout" and the
 * hidden-widget count act on whichever tab is open.
 */
export const FLEET_DASHBOARD_STORAGE_KEY = 'farm-fleet-dashboard-layout-v1';

/**
 * The hours-down band edges for a DAY bucket, in hours: `<1 · 1–4 · 4–12 · 12+`.
 *
 * ABSOLUTE, not relative to the window: four hours down is four hours down
 * whether the rest of the fleet had a good month or a terrible one, and a
 * relative ramp would paint a perfect week's worst day the same red as an
 * outage. Lower-inclusive, upper-exclusive throughout.
 */
export const HOURS_DOWN_DAY_EDGES: readonly [number, number, number] = [1, 4, 12];

/** The same edges scaled to a bucket's width — week ×7, hour ÷24. */
export function hoursDownEdges(bucket: FleetBucket): [number, number, number] {
  const scale = BUCKET_HOURS[bucket] / BUCKET_HOURS.day;
  return [
    HOURS_DOWN_DAY_EDGES[0] * scale,
    HOURS_DOWN_DAY_EDGES[1] * scale,
    HOURS_DOWN_DAY_EDGES[2] * scale,
  ];
}

/** Which heat step `hours` of downtime falls in for this bucket width. */
export function hoursDownBand(hours: number, bucket: FleetBucket): HeatIndex {
  if (!(hours > 0)) return 0;
  const [low, mid, high] = hoursDownEdges(bucket);
  if (hours < low) return 1;
  if (hours < mid) return 2;
  if (hours < high) return 3;
  return 4;
}

/**
 * Which heat step a print count falls in, RELATIVE to the window's busiest
 * bucket. Prints have no absolute "bad" the way downtime does — a farm's good
 * day is whatever that farm's good day is — so this ramp rescales per window
 * and the legend states the maximum it was scaled against.
 */
export function printsBand(value: number, windowMax: number): HeatIndex {
  if (!(value > 0) || !(windowMax > 0)) return 0;
  const step = Math.ceil((value / windowMax) * 4);
  return Math.min(4, Math.max(1, step)) as HeatIndex;
}

/** The Time-split lens's three bands, plus the remainder nothing is known about. */
export interface TimeSplit {
  printing: number;
  /** Between prints, idle and maintenance — counted time that was not printing. */
  other: number;
  down: number;
  /** Unobserved, before recording and out of fleet: not a state, an absence. */
  noData: number;
  /** The four above. Equals the cell's elapsed seconds for a consistent response. */
  total: number;
}

/**
 * Fold a sparse class-seconds map into the three bands the matrix draws, plus
 * the no-data remainder.
 *
 * The backend's identity is that a printer-bucket's class seconds sum to its
 * ELAPSED seconds, so this fold is exhaustive by construction: every key lands
 * in exactly one band and `total` is the map's own sum. A band computed by
 * subtracting two others would hide a key this build does not know about.
 */
export function foldTimeSplit(classSeconds: Partial<Record<FleetClassKey, number>>): TimeSplit {
  let printing = 0;
  let other = 0;
  let down = 0;
  let noData = 0;
  for (const [key, seconds] of Object.entries(classSeconds)) {
    if (!seconds) continue;
    const { group } = parseClassKey(key);
    switch (group) {
      case 'printing':
        printing += seconds;
        break;
      case 'down':
        down += seconds;
        break;
      case 'out_of_fleet':
      case 'not_recorded':
      case 'unobserved':
        noData += seconds;
        break;
      default:
        other += seconds;
    }
  }
  return { printing, other, down, noData, total: printing + other + down + noData };
}

/** Sum a sparse map. */
export function sumMap(map: Partial<Record<string, number>>): number {
  let total = 0;
  for (const value of Object.values(map)) total += value ?? 0;
  return total;
}

// ── absence ─────────────────────────────────────────────────────────────────

/**
 * How short of a bucket's elapsed time the recorder may fall before the cell is
 * called partly observed. The recorder samples every 30 s and a span is closed
 * at its own last observation, so a bucket can legitimately be a couple of
 * samples short at each edge without anything having gone wrong.
 */
export const OBSERVED_TOLERANCE_S = 90;

/**
 * The four ways a cell can have no ordinary number, plus `null` for one that
 * does. Never conflated — a zero the farm earned and a zero nobody recorded are
 * different answers, and on a lights-out farm the difference is the whole point.
 */
export type CellAbsence = 'zero' | 'before_recording' | 'partial' | 'out_of_fleet' | null;

/** Enough of a bucket header for an absence verdict. */
export type AbsenceHeader = Pick<
  SeriesBucket<unknown>,
  'elapsed_seconds' | 'observed_seconds'
>;

export interface CellAbsenceOptions {
  lens: FleetLens;
}

/**
 * Which absence treatment one cell takes under one lens.
 *
 * The lens is a PARAMETER and not a caller's special case, because the rule is
 * about where the data comes from, not about who is drawing: `before_recording`
 * and `partial` are verdicts on the STATE recorder, and the Prints lens is fed
 * by the print log, which is complete for its own history. A dash or a hatch
 * over a real print count would be the tab telling a lie in the safe direction.
 * That is the same rule the backend states by withholding `basis` from its
 * print-derived series.
 */
export function cellAbsence(
  cell: MatrixCell,
  header: AbsenceHeader,
  { lens }: CellAbsenceOptions,
): CellAbsence {
  const total = sumMap(cell.class_seconds);
  const stateDerived = lens !== 'prints';

  if (total > 0 && (cell.class_seconds.out_of_fleet ?? 0) >= total - OBSERVED_TOLERANCE_S) {
    return 'out_of_fleet';
  }
  if (
    stateDerived &&
    total > 0 &&
    (cell.class_seconds.not_recorded ?? 0) >= total - OBSERVED_TOLERANCE_S
  ) {
    return 'before_recording';
  }
  if (stateDerived) {
    const bucketShort = header.observed_seconds < header.elapsed_seconds - OBSERVED_TOLERANCE_S;
    if (cell.basis === 'incidents_only' || bucketShort) return 'partial';
  }
  return lensValue(cell, lens) > 0 ? null : 'zero';
}

/** The number a lens reads out of a cell. */
export function lensValue(cell: MatrixCell, lens: FleetLens): number {
  switch (lens) {
    case 'prints':
      return sumMap(cell.prints);
    case 'hours_down':
      return cell.down_seconds;
    default:
      return sumMap(cell.class_seconds);
  }
}

/**
 * Is this row hidden altogether?
 *
 * A printer that was DELETED from the roster and was already out of the fleet
 * for every bucket in the window has nothing to say about the window: its row
 * would be a name the operator no longer recognises over a line of flat ground.
 * A deleted printer that WAS in the fleet for part of the window keeps its row
 * — that history is exactly what a "which printer is my problem" question needs.
 */
export function isRowHidden(
  printer: Pick<MatrixPrinter, 'deleted'>,
  cells: readonly (MatrixCell | undefined)[],
): boolean {
  if (!printer.deleted) return false;
  return cells.every((cell) => {
    if (!cell) return true;
    const total = sumMap(cell.class_seconds);
    if (total <= 0) return true;
    return (cell.class_seconds.out_of_fleet ?? 0) >= total - OBSERVED_TOLERANCE_S;
  });
}

// ── format ──────────────────────────────────────────────────────────────────
//
// Every formatter takes the ACTIVE i18n language and formats through `Intl`, so
// a German operator reads `31,4` and a decimal comma never has to be hand-rolled.

const numberFormat = (locale: string, min: number, max: number): Intl.NumberFormat =>
  new Intl.NumberFormat(locale, { minimumFractionDigits: min, maximumFractionDigits: max });

/**
 * Hours: one decimal below 10, integer above. A matrix column is 28 px wide, so
 * `31.4` and `4.0` both have to fit; past 10 hours the decimal is noise anyway.
 */
export function formatHours(hours: number, locale: string): string {
  return Math.abs(hours) < 10
    ? numberFormat(locale, 1, 1).format(hours)
    : numberFormat(locale, 0, 0).format(hours);
}

/** Average printers: always one decimal, so "2.1 down" reads as an average. */
export function formatPrinters(value: number, locale: string): string {
  return numberFormat(locale, 1, 1).format(value);
}

/** A whole count. */
export function formatCount(value: number, locale: string): string {
  return numberFormat(locale, 0, 0).format(value);
}

/** A 0–1 ratio as an integer percentage. */
export function formatPercent(ratio: number, locale: string): string {
  return new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 0 }).format(ratio);
}

/**
 * A change between two ratios, in percentage POINTS — the only honest unit for
 * "uptime went from 91 % to 89 %". Sign is left to the caller's arrow.
 */
export function formatPoints(deltaRatio: number, locale: string): string {
  return numberFormat(locale, 0, 0).format(Math.abs(deltaRatio) * 100);
}

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

const pad = (value: number, locale: string): string =>
  new Intl.NumberFormat(locale, {
    minimumIntegerDigits: 2,
    maximumFractionDigits: 0,
    useGrouping: false,
  }).format(value);

/**
 * A duration as an operator reads one off a card: `2 d 4 h`, `4 h 02 m`,
 * `1 m 25 s`, `42 s`. The leading component is exact and the second is padded,
 * so a column of them lines up.
 *
 * The unit letters are symbols, not copy — `d` / `h` / `m` / `s` ship verbatim
 * in every locale the app has, the way `°C` and `mm` do. The LOCALE governs the
 * digits.
 */
export function formatDuration(seconds: number, locale: string): string {
  const total = Math.max(0, Math.round(seconds));
  if (total >= DAY) {
    return `${formatCount(Math.floor(total / DAY), locale)} d ${formatCount(Math.floor((total % DAY) / HOUR), locale)} h`;
  }
  if (total >= HOUR) {
    return `${formatCount(Math.floor(total / HOUR), locale)} h ${pad(Math.floor((total % HOUR) / MINUTE), locale)} m`;
  }
  if (total >= MINUTE) {
    return `${formatCount(Math.floor(total / MINUTE), locale)} m ${pad(total % MINUTE, locale)} s`;
  }
  return `${formatCount(total, locale)} s`;
}

/** Which way a figure moved. No good/bad semantics: up is up. */
export type ChangeDirection = 'up' | 'down' | 'none';

export interface ChangeReading {
  direction: ChangeDirection;
  /** The absolute difference, in the row's own unit. */
  magnitude: number;
  /** The i18n key for the spelled-out accessible sentence. */
  labelKey: string;
}

export interface ChangeOptions {
  /**
   * Decimal places the figure is DISPLAYED with. Both sides are rounded to it
   * before comparing, so a "▲" never appears beside two numbers a reader can
   * see are the same.
   */
  precision?: number;
}

/**
 * Read a change against the previous window. Null when either side is missing —
 * a window the recorder did not cover has no previous to compare with, and a
 * zero would read as "no change", which is a different claim.
 */
export function readChange(
  figure: number | null,
  previous: number | null,
  { precision = 1 }: ChangeOptions = {},
): ChangeReading | null {
  if (figure === null || previous === null) return null;
  const factor = 10 ** precision;
  const now = Math.round(figure * factor) / factor;
  const before = Math.round(previous * factor) / factor;
  const magnitude = Math.abs(now - before);
  if (now === before) {
    return { direction: 'none', magnitude: 0, labelKey: 'fleetMetrics.change.none' };
  }
  return now > before
    ? { direction: 'up', magnitude, labelKey: 'fleetMetrics.change.up' }
    : { direction: 'down', magnitude, labelKey: 'fleetMetrics.change.down' };
}

// ── series adapter ──────────────────────────────────────────────────────────

/** The site-local label parts for one bucket. */
export interface BucketLabel {
  /** `YYYY-MM-DD` in the SITE's zone. */
  date: string;
  /** Narrow weekday, e.g. `M`. */
  weekdayInitial: string;
  /** Day of month, locale-formatted. */
  dayOfMonth: string;
  /** Short month name — the first bucket, and every bucket that starts a new one. */
  month: string | null;
  /** `HH:MM` for an hour bucket; null otherwise. */
  hour: string | null;
  /** A week bucket's own start date. A CLIPPED first week keeps its real start. */
  weekStart: string | null;
  isWeekend: boolean;
  /** "now" falls inside this bucket: it has elapsed time but is not yet full. */
  isCurrent: boolean;
  /** The full site-local date (and time, for an hour bucket) for the sr-only header. */
  full: string;
}

/** Everything a chart row carries besides its own values. */
export interface SeriesRowMeta {
  /** The bucket's `start`, verbatim — a stable React key and the detail's argument. */
  bucketStart: string;
  bucketIndex: number;
  bucketLabel: BucketLabel;
  bucketSeconds: number;
  bucketElapsedSeconds: number;
  bucketObservedSeconds: number;
  bucketBasis: FleetBasis | null;
  /** The recorder covered less of this bucket than elapsed: sum charts hatch it. */
  bucketPartial: boolean;
}

export type SeriesRow<Values extends object> = SeriesRowMeta & Values;

export interface SeriesRowsOptions {
  /** The bucket the server ECHOED — never the one the client asked for. */
  bucket: FleetBucket;
  /** The active i18n language, for the weekday and month names. */
  locale: string;
}

/**
 * Shift a naive-UTC instant by the site's offset AT that instant, so reading it
 * with UTC getters yields the SITE's wall clock.
 *
 * This is the whole timezone story of the Fleet tab, and it is deliberately
 * bucket-independent: the server stamps each bucket with the offset that was in
 * force when it started, so a DST transition inside a window is correct on both
 * sides of the change and the browser's own zone never enters the calculation.
 * A label rendered in Kiritimati and one rendered in Los Angeles are identical.
 */
function siteInstant(start: string, utcOffsetMinutes: number): Date {
  const utcMs = Date.parse(`${start}Z`);
  return new Date(utcMs + utcOffsetMinutes * 60_000);
}

const UTC = 'UTC';

function siteDateKey(shifted: Date): string {
  return shifted.toISOString().slice(0, 10);
}

/** The label parts for one bucket. `previous` supplies the month-at-change rule. */
export function bucketLabel(
  bucket: SeriesBucket<unknown>,
  options: SeriesRowsOptions,
  previous?: SeriesBucket<unknown>,
): BucketLabel {
  const { bucket: width, locale } = options;
  const shifted = siteInstant(bucket.start, bucket.utc_offset_minutes);
  const date = siteDateKey(shifted);
  const month = new Intl.DateTimeFormat(locale, { month: 'short', timeZone: UTC }).format(shifted);
  const previousMonth =
    previous === undefined
      ? null
      : new Intl.DateTimeFormat(locale, { month: 'short', timeZone: UTC }).format(
          siteInstant(previous.start, previous.utc_offset_minutes),
        );
  const weekday = shifted.getUTCDay();
  const isHour = width === 'hour';
  return {
    date,
    weekdayInitial: new Intl.DateTimeFormat(locale, { weekday: 'narrow', timeZone: UTC }).format(shifted),
    dayOfMonth: new Intl.DateTimeFormat(locale, { day: 'numeric', timeZone: UTC }).format(shifted),
    month: previousMonth === null || previousMonth !== month ? month : null,
    hour: isHour
      ? new Intl.DateTimeFormat(locale, {
          hour: '2-digit',
          minute: '2-digit',
          hour12: false,
          timeZone: UTC,
        }).format(shifted)
      : null,
    weekStart: width === 'week' ? date : null,
    isWeekend: weekday === 0 || weekday === 6,
    isCurrent: bucket.elapsed_seconds > 0 && bucket.elapsed_seconds < bucket.seconds,
    full: new Intl.DateTimeFormat(locale, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      ...(isHour ? { hour: '2-digit', minute: '2-digit', hour12: false } : {}),
      timeZone: UTC,
    }).format(shifted),
  };
}

/**
 * THE adapter: any `SeriesEnvelope<T>` becomes recharts rows.
 *
 * One function and not one per widget, because every chart on this tab needs
 * the same four things — a stable row key, the site-local label parts, the
 * partly-observed flag that decides whether the bar is hatched, and the values.
 * `pick` returns a FLAT object that is spread into the row, so a recharts
 * `dataKey` is a plain top-level name.
 *
 * `pick` must not return any of the `bucket*` meta names.
 */
export function seriesRows<Values, Picked extends object>(
  envelope: SeriesEnvelope<Values>,
  pick: (values: Values, bucket: SeriesBucket<Values>) => Picked,
  options: SeriesRowsOptions,
): SeriesRow<Picked>[] {
  return envelope.buckets.map((bucket, index) => ({
    bucketStart: bucket.start,
    bucketIndex: index,
    bucketLabel: bucketLabel(bucket, options, index === 0 ? undefined : envelope.buckets[index - 1]),
    bucketSeconds: bucket.seconds,
    bucketElapsedSeconds: bucket.elapsed_seconds,
    bucketObservedSeconds: bucket.observed_seconds,
    bucketBasis: bucket.basis,
    bucketPartial: bucket.observed_seconds < bucket.elapsed_seconds - OBSERVED_TOLERANCE_S,
    ...pick(bucket.values, bucket),
  }));
}
