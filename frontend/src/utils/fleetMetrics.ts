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

import type { PrinterIncidentKind } from '../api/client';
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

/**
 * Summary row → the live class GROUP whose printer count is that row's "Now".
 *
 * The pairing is what makes the two figure columns comparable: `avg_down` is
 * average concurrent printers down, and `counts_by_group.down` is printers down
 * at this instant. A row absent from this map has no instantaneous twin (a rate
 * per day, a percentage) and leaves Now blank.
 */
export const SUMMARY_ROW_NOW_GROUP: Partial<Record<SummaryRowKey, FleetGroup>> = {
  avg_printing: 'printing',
  avg_cycle_overhead: 'cycle_overhead',
  avg_idle: 'idle',
  avg_down: 'down',
  avg_planned: 'planned',
};

/**
 * Every summary row the backend composes, in the order it composes them.
 *
 * The card renders this list BEFORE the history sweep answers, with empty
 * figures, so that the card it draws at first paint is the same shape as the
 * card it draws when the numbers land. It used to fall back to the five live
 * rows plus "In fleet", and the card then grew by 89 px when `/overview`
 * returned — shoving the matrix and the whole grid below it down the page.
 */
export const SUMMARY_ROW_ORDER: readonly SummaryRowKey[] = [
  'avg_printing',
  'avg_cycle_overhead',
  'avg_idle',
  'avg_down',
  'avg_planned',
  'peak_down',
  'printers_in_fleet',
  'prints_per_day',
  'prints_per_printer_per_day',
  'uptime',
  'time_printing',
];

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

/**
 * Every hint leaf one summary row carries, in reading order.
 *
 * The second one is CONDITIONAL on the window: where the recorder did not cover
 * all of it, the period column is an average over the part it DID cover, so a
 * reader setting that figure beside the live Now column is comparing two
 * different spans. The qualifier rides the tooltip the row already has — a row
 * with nothing else to explain does not gain one just to carry it, and none of
 * it goes inline on the primary surface (react-best-practices §9).
 *
 * Every hinted row is state-derived, `prints_per_printer_per_day` included: its
 * denominator is the days each printer was COUNTED, which the recorder states.
 */
export function summaryHintKeys(
  key: SummaryRowKey,
  { recordingGap }: { recordingGap: boolean },
): string[] {
  const own = SUMMARY_ROW_HINT_KEY[key];
  if (own === undefined) return [];
  return recordingGap ? [own, 'fleetMetrics.hints.whileRecorded'] : [own];
}

/**
 * The unit a row's figure cannot be read without, or nothing where the row
 * label already carries it.
 *
 * `SUMMARY_ROW_FORMAT` says how a figure is SPELLED — one decimal, a whole
 * count, a percentage — and deliberately says nothing about what it counts per.
 * The two rate rows are per DAY, and a bare "Prints 21" beside a matrix total
 * of 900 is not a figure a reader can place: the unit is what makes it one.
 */
export const SUMMARY_ROW_UNIT_KEY: Partial<Record<SummaryRowKey, string>> = {
  prints_per_day: 'fleetMetrics.units.perDay',
  prints_per_printer_per_day: 'fleetMetrics.units.perDay',
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

/**
 * How the FLEET row's Avg cell combines the printers' own per-day rates.
 *
 * Not one answer, because the lenses ask different questions of that column.
 * Under **Prints** the fleet's output IS the sum — twelve printers making six
 * prints a day each is a farm making seventy-two a day, which is the figure an
 * operator plans against. Under **Hours down** the sum is not a rate anybody
 * can read: it printed 86 "h/day" on a twelve-printer farm, and a day has
 * twenty-four hours, so the one column meant to be comparable with the rows
 * above it was the one column that could not be. The mean per printer can be,
 * and by construction never exceeds a day.
 *
 * Time split's Avg carries hours down per day too (a share has no per-day twin
 * in the payload), so it follows Hours down.
 */
export type FleetAvgMode = 'sum' | 'mean';

export const LENS_FLEET_AVG_MODE: Record<FleetLens, FleetAvgMode> = {
  prints: 'sum',
  hours_down: 'mean',
  time_split: 'mean',
};

/** The leaf that says WHICH of the two the Fleet row's Avg cell is. */
export const FLEET_AVG_DESCRIPTION_KEY: Record<FleetAvgMode, string> = {
  sum: 'fleetMetrics.matrix.fleetAvgSum',
  mean: 'fleetMetrics.matrix.fleetAvgMean',
};

/**
 * The Fleet row's Avg, from the printers' own per-day rates.
 *
 * The mean divides by the rows that HAVE a rate, not by every row shown: a
 * printer the recorder never counted has no denominator and prints "–" in its
 * own Avg cell, and averaging it in as a zero is exactly the unknown-read-as-
 * zero conflation the rest of this module exists to prevent. Where every
 * printer has a figure — the case the ≤ 24 bound is argued from — the two
 * denominators are the same number.
 *
 * Null when no printer has a rate yet: a mean of nothing is not zero.
 */
export function fleetAverage(
  rates: readonly (number | null)[],
  mode: FleetAvgMode,
): number | null {
  let total = 0;
  let counted = 0;
  for (const rate of rates) {
    if (rate === null) continue;
    total += rate;
    counted += 1;
  }
  if (counted === 0) return null;
  return mode === 'sum' ? total : total / counted;
}


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
 * Print outcome → band colour, COMPOSED from the class palette rather than
 * typed as new hexes: a completed print is the green that already means
 * printing, a failure is the red that already means down, an operator stop is
 * the amber that already means a person intervened, and "other" is the
 * neutral. Nothing new to validate for contrast, and nothing to drift.
 */
export const OUTCOME_COLOR: Record<PrintOutcome, string> = {
  completed: FLEET_GROUP_COLOR.printing,
  failed: FLEET_GROUP_COLOR.down,
  cancelled: FLEET_GROUP_COLOR.planned,
  other: FLEET_GROUP_COLOR.idle,
};

/** The text colour proven legible on each outcome band, inherited with it. */
export const OUTCOME_TEXT: Record<PrintOutcome, string> = {
  completed: FLEET_GROUP_TEXT.printing,
  failed: FLEET_GROUP_TEXT.down,
  cancelled: FLEET_GROUP_TEXT.planned,
  other: FLEET_GROUP_TEXT.idle,
};

/**
 * The SKU bands, composed from the same class palette. Red is LAST of the five
 * so it only appears on a farm running five or more SKUs at once, where the
 * legend is doing the identifying anyway; nothing in that widget draws a
 * printer state, so the hues carry no second meaning there.
 */
export const SKU_BAND_COLORS: readonly string[] = [
  FLEET_GROUP_COLOR.printing,
  FLEET_GROUP_COLOR.cycle_overhead,
  FLEET_GROUP_COLOR.planned,
  FLEET_GROUP_COLOR.idle,
  FLEET_GROUP_COLOR.down,
];

/** The text colour proven legible on each SKU band, in the same order. */
export const SKU_BAND_TEXT: readonly string[] = [
  FLEET_GROUP_TEXT.printing,
  FLEET_GROUP_TEXT.cycle_overhead,
  FLEET_GROUP_TEXT.planned,
  FLEET_GROUP_TEXT.idle,
  FLEET_GROUP_TEXT.down,
];

/** One SKU band's colour. The "other" remainder takes the neutral absence swatch. */
export function skuBandColor(index: number, isOther: boolean): string {
  if (isOther) return FLEET_ABSENCE_COLOR.unobserved;
  return SKU_BAND_COLORS[index % SKU_BAND_COLORS.length] ?? FLEET_ABSENCE_COLOR.unobserved;
}

/** The text colour proven legible on that SKU band. */
export function skuBandText(index: number, isOther: boolean): string {
  if (isOther) return FLEET_ABSENCE_TEXT.unobserved;
  return SKU_BAND_TEXT[index % SKU_BAND_TEXT.length] ?? FLEET_ABSENCE_TEXT.unobserved;
}

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

/** The one DECLARED incident kind: planned work, never an equipment fault. */
const SERVICE_HOLD_KIND: PrinterIncidentKind = 'service_hold';

/**
 * The down cause an incident kind classifies to.
 *
 * The classifier spells a fault as `fault:<kind>`, so this is the one place the
 * prefix is applied to a ledger kind — an incident-derived surface can then
 * reach the cause's label, colour and shade through the ordinary lookups
 * instead of inventing a parallel vocabulary for the same twelve facts.
 */
export function incidentKindCause(kind: PrinterIncidentKind): FleetCause {
  return `${FAULT_PREFIX}${kind}` as FleetCause;
}

/**
 * An incident kind's band colour.
 *
 * A declared `service_hold` takes the MAINTENANCE hue rather than a fault
 * shade: it is planned work, and colouring it red would make a deliberate
 * service window read as a breakdown in every chart that stacks the two.
 */
export function incidentKindColor(kind: PrinterIncidentKind): string {
  return kind === SERVICE_HOLD_KIND
    ? FLEET_GROUP_COLOR.planned
    : downCauseColor(incidentKindCause(kind));
}

/** The text colour proven legible on that kind's band. */
export function incidentKindTextColor(kind: PrinterIncidentKind): string {
  return kind === SERVICE_HOLD_KIND
    ? FLEET_GROUP_TEXT.planned
    : downCauseTextColor(incidentKindCause(kind));
}

/**
 * The i18n key that NAMES an incident kind.
 *
 * Not `causeLabelKey(incidentKindCause(kind))`, which is what the Recovery
 * widget used to spell: that builds `fault:service_hold` for the one DECLARED
 * kind and so asks for `printers.incident.service_hold`, a leaf that does not
 * exist — the legend, the series name and the data-table header all printed the
 * raw key. The two colour lookups above have always made the declared/fault
 * distinction; the label lookup has to make the same one, and it is made HERE
 * so that no consumer has to remember it a fourth time.
 */
export function incidentKindLabelKey(kind: PrinterIncidentKind): string {
  return kind === SERVICE_HOLD_KIND
    ? GROUP_LABEL_KEY.planned
    : causeLabelKey(incidentKindCause(kind));
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

/**
 * A CSS background — an HTML table cell needs no SVG to carry a pattern.
 *
 * Repeat and position are optional because a full-surface texture needs
 * neither; a texture confined to one EDGE of a surface needs both, and leaving
 * them to the call site is how one cell ends up with the band and the next with
 * the band tiled over its whole face.
 */
export interface PatternCss {
  backgroundImage: string;
  backgroundSize: string;
  backgroundRepeat?: string;
  backgroundPosition?: string;
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

/**
 * How tall the partly-observed BAND is, in px, along a cell's bottom edge.
 *
 * Four: two hatch strokes' worth at the sparse pattern's 12 px period, which is
 * the least that still reads as a texture rather than as a rule, inside a 24 px
 * cell that has a figure to show.
 */
export const FLEET_PARTIAL_BAND_PX = 4;

/**
 * The sparse hatch confined to a band along the bottom edge.
 *
 * The full-face hatch strokes in `currentColor`, which on a heat tile IS the
 * figure's own colour — so the stripes ran THROUGH the digits, and on the
 * saturated end of the down ramp a `12` was unreadable behind them. The marker
 * and the figure want the same square of cell and the figure wins: the texture
 * moves to the edge, the digits keep a clean tile, and the contrast the ramp
 * was measured for is the contrast the reader gets.
 *
 * `100%` wide and `FLEET_PARTIAL_BAND_PX` tall pins the gradient's box to that
 * strip (a gradient has no intrinsic size, so `auto` would resolve to the whole
 * cell), `no-repeat` stops it tiling back up the face, and `left bottom` puts
 * it where a footnote goes.
 */
export const FLEET_PARTIAL_BAND_CSS: PatternCss = {
  backgroundImage: FLEET_PATTERN_CSS.sparse.backgroundImage,
  backgroundSize: `100% ${FLEET_PARTIAL_BAND_PX}px`,
  backgroundRepeat: 'no-repeat',
  backgroundPosition: 'left bottom',
};

/**
 * The partly-observed marker for one cell: the bottom band where the cell shows
 * a figure, the full hatch where it has none.
 *
 * A cell with nothing to show has no digits to protect and the whole face is
 * the clearest way to say "this square is not evidence"; a cell WITH a figure
 * is making a claim, and the marker qualifies it rather than obscuring it.
 */
export function partialMarkerCss(showsFigure: boolean): PatternCss {
  return showsFigure ? FLEET_PARTIAL_BAND_CSS : FLEET_PATTERN_CSS.sparse;
}

/**
 * The marker for a bucket that is still IN PROGRESS — a half-filled swatch,
 * not a pattern.
 *
 * Deliberately not one of the three patterns: "today so far" and "the recorder
 * was not watching" are different claims (see `SeriesRowMeta`), and a table row
 * can carry both at once. A pattern would say the second thing about a bucket
 * where only the first is true. Half filled reads as "half a bucket", which is
 * exactly what it is, and `currentColor` keeps its stroke on whatever text
 * colour the surface already proved legible.
 */
export const FLEET_IN_PROGRESS_CSS: PatternCss = {
  backgroundImage: 'linear-gradient(90deg, currentColor 0 50%, transparent 50%)',
  backgroundSize: 'auto',
};

/**
 * Print outcome → its pattern: the SECOND encoding channel for the outcome
 * stack, the one the state chart has always had and this one did not.
 *
 * Hue alone is not identity. Under deuteranopia the outcome bands' green, red
 * and amber collapse into near-identical olive, and a reader cannot tell a
 * failed print from one an operator stopped — which is the single distinction
 * the chart exists to draw. The two that matter therefore carry a texture as
 * well: failed hatches (the same 45° hatch that already means "down"),
 * cancelled takes the dots that already mean "a person intervened". `completed`
 * stays plain, so the good case is the quiet one and the textures read as
 * exceptions rather than as noise.
 *
 * Every surface that names a band draws the same pattern: the bar, the legend
 * swatch and the tooltip swatch.
 */
export const OUTCOME_PATTERN: Partial<Record<PrintOutcome, FleetPattern>> = {
  failed: 'hatch',
  cancelled: 'dots',
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
 * The matrix's FROZEN block: the width of each column beside the printer name,
 * in px, and nothing else allowed to state it.
 *
 * These are the numbers a `<colgroup>` hands the table AND the numbers each
 * sticky cell's `left` offset is summed from (`matrixFrozenLeft`). They were
 * two hand-synchronised things — Tailwind width utilities (`w-28`, `@2xl:w-40`)
 * on the cells and literal offsets (`left-28`, `left-[10.5rem]`) — and under
 * `table-layout: auto` the widths were merely ADVISORY, so the columns
 * collapsed to their content (54 px, not 112) while the offsets stayed at the
 * nominal figure. The frozen cells detached from each other and day columns
 * rendered in the gaps. One definition, in px, makes that unrepresentable.
 *
 * Deliberately NOT responsive. A second set switched at a container query would
 * put the breakpoint back into two places (the `<col>` and the offsets) and is
 * what the bug was made of; 128 px holds the fleet's names ("001-H2S",
 * "Printer 12") at every width, and a longer one truncates with its full text
 * on the cell — react-best-practices §9.
 */
export const MATRIX_FROZEN_WIDTH_PX = { printer: 128, total: 56, avg: 56 } as const;

/** The frozen columns in their DOM order. The `left` offsets are their prefix sums. */
export const MATRIX_FROZEN_COLUMNS = ['printer', 'total', 'avg'] as const;

export type MatrixFrozenColumn = (typeof MATRIX_FROZEN_COLUMNS)[number];

/**
 * How far from the scroller's left edge a frozen column is pinned: the sum of
 * the widths BEFORE it. Derived, never typed — the first column is flush at 0
 * and each next one begins exactly where the previous ended, which is the whole
 * definition of "contiguous".
 */
export function matrixFrozenLeft(column: MatrixFrozenColumn): number {
  let left = 0;
  for (const name of MATRIX_FROZEN_COLUMNS) {
    if (name === column) return left;
    left += MATRIX_FROZEN_WIDTH_PX[name];
  }
  return left;
}

/** Total width of the frozen block — where the first bucket column begins. */
export const MATRIX_FROZEN_TOTAL_PX = MATRIX_FROZEN_COLUMNS.reduce(
  (total, name) => total + MATRIX_FROZEN_WIDTH_PX[name],
  0,
);


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
 * The five ways a cell's number is qualified, plus `null` for one that is not.
 * Never conflated — a zero the farm earned and a zero nobody recorded are
 * different answers, and on a lights-out farm the difference is the whole point.
 *
 * The two PARTLY-observed verdicts were one (`partial`) and had to be split.
 * The cell named itself from the bucket's `basis`, and `observed` mapped to the
 * "No data" leaf — so a hatched Hours-down cell showing a real `12` announced
 * "No data" beside its own figure. They are different claims and each carries
 * its own name now:
 *
 *   - `incidents_only` — the recorder covered NONE of the bucket, and the fault
 *     and hold ledger is the whole of the evidence. The hours are real.
 *   - `partly_observed` — the recorder covered some of it and fell short.
 *
 * `upcoming` is the sixth and is not an absence of DATA at all — it is the
 * absence of the hour. On "Today" at 17:00, seven of the twenty-four buckets
 * have not started, and the server says so with `elapsed_seconds: 0`. They read
 * as ledger-only zeros: a full hatch and an sr-only "0 Faults and holds only",
 * over a drill-down that correctly reported no data for the range.
 *
 * A verdict is a statement about the EVIDENCE, never about the value: three of
 * the six sit happily beside a figure.
 */
export type CellAbsence =
  | 'zero'
  | 'upcoming'
  | 'before_recording'
  | 'incidents_only'
  | 'partly_observed'
  | 'out_of_fleet'
  | null;

/**
 * The i18n leaf that NAMES a verdict — the one place a cell's spoken reason is
 * decided, so no surface can pair a reason with a figure that contradicts it.
 *
 * `zero` and `null` are absent on purpose: a cell whose figure is its own
 * complete answer says nothing extra, and an empty entry here would invite one.
 */
export const CELL_ABSENCE_LABEL_KEY: Record<
  Exclude<CellAbsence, null | 'zero'>,
  string
> = {
  before_recording: 'fleetMetrics.class.not_recorded',
  incidents_only: 'fleetMetrics.class.incidents_only',
  // Reused from the chart data tables, where the same bucket wears the same
  // word: one fact, one leaf, translated once.
  partly_observed: 'fleetMetrics.widgets.partlyObserved',
  out_of_fleet: 'fleetMetrics.class.out_of_fleet',
  upcoming: 'fleetMetrics.class.upcoming',
};

/** Does this verdict draw the partly-observed marker? */
export function isPartlyObserved(absence: CellAbsence): boolean {
  return absence === 'incidents_only' || absence === 'partly_observed';
}

/** Enough of a bucket header for an absence verdict. */
export type AbsenceHeader = Pick<
  SeriesBucket<unknown>,
  'elapsed_seconds' | 'observed_seconds'
>;

/**
 * Has this bucket happened at all?
 *
 * THE predicate for it, so the threshold is stated once and the cell, the
 * verdict, the row builders and the drill-down can never disagree about which
 * buckets are real. A window is a grid, and a grid over "Today" carries the
 * hours that have not arrived yet.
 */
export function bucketHasElapsed(header: Pick<AbsenceHeader, 'elapsed_seconds'>): boolean {
  return header.elapsed_seconds > 0;
}

/**
 * The index of the bucket "now" falls in — the last one with elapsed time.
 *
 * A window wholly in the past has no future buckets, so this is simply its last
 * one; a window ending today stops at the bucket in progress. The hour grid's
 * per-row Details control is what needs it: it opened `buckets.length - 1`,
 * which on Today is 23:00, so a printer with seventeen hours down today opened
 * an empty dialog for an hour that has not happened.
 */
export function currentBucketIndex(
  buckets: readonly Pick<AbsenceHeader, 'elapsed_seconds'>[],
): number {
  for (let index = buckets.length - 1; index >= 0; index -= 1) {
    const bucket = buckets[index];
    if (bucket !== undefined && bucketHasElapsed(bucket)) return index;
  }
  return Math.max(0, buckets.length - 1);
}

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
  // FIRST, before anything reads a class map: a bucket that has not started
  // cannot be zero, cannot be unobserved and cannot be evidence. Every other
  // verdict below is an answer about DATA, and there is no data question to
  // ask about an hour that has not arrived.
  if (!bucketHasElapsed(header)) return 'upcoming';

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
    // Asked in this order because they are not the same claim and the stronger
    // one wins: a bucket the recorder never reached is `incidents_only`
    // whatever its observed seconds say, and only a bucket it DID reach can be
    // short of one.
    if (cell.basis === 'incidents_only') return 'incidents_only';
    if (header.observed_seconds < header.elapsed_seconds - OBSERVED_TOLERANCE_S) {
      return 'partly_observed';
    }
  }
  return lensValue(cell, lens) > 0 ? null : 'zero';
}

/**
 * COMPLETED prints in a cell — the Prints lens's one numerator.
 *
 * Not `sumMap(cell.prints)`, which is every outcome. The lens answers "what did
 * this printer make", and a failed or cancelled print made nothing; the tab's
 * other print figures already agree — `prints_per_day` and
 * `prints_per_printer_per_day` are both completed-only, and the matrix's Avg
 * column reads them straight from the payload. Summing all four outcomes in the
 * cells and the row Total put TWO numerators in one lens: a printer showed
 * Total 1,079 against an Avg of 6.0, which is not a rate anybody could derive
 * from the row they were looking at.
 *
 * The full outcome split is not lost — it is in a bucket's detail, under
 * "Prints by outcome", which is the surface that exists to carry it.
 */
export function completedPrints(cell: MatrixCell): number {
  return cell.prints.completed ?? 0;
}

/** The number a lens reads out of a cell. */
export function lensValue(cell: MatrixCell, lens: FleetLens): number {
  switch (lens) {
    case 'prints':
      return completedPrints(cell);
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

// ── site time ───────────────────────────────────────────────────────────────
//
// THE Fleet tab's timezone story, in one place. Three shapes, because three
// different things arrive on the wire and each needs a different conversion —
// but all three obey one rule: **a label never depends on the browser's zone.**
// A page rendered in Kiritimati and one rendered in Los Angeles read the same.
//
// Every component that spelled one of these privately now imports it; a second
// conversion is a second answer, and the two disagree on exactly the days that
// matter (a DST change, a site ahead of UTC, the bucket that spans midnight).

const MS_PER_MINUTE = 60_000;

const UTC = 'UTC';

/**
 * Shift a naive-UTC instant by the site's offset AT that instant, so reading it
 * with UTC getters yields the SITE's wall clock.
 *
 * Deliberately bucket-independent: the server stamps each bucket with the
 * offset that was in force when it started, so a DST transition inside a window
 * is correct on both sides of the change.
 */
function siteInstant(start: string, utcOffsetMinutes: number): Date {
  return new Date(Date.parse(`${start}Z`) + utcOffsetMinutes * MS_PER_MINUTE);
}

/**
 * How an instant is spelled when the surface has the site's OFFSET in hand
 * (every series bucket carries one).
 *
 * The date is always part of it, never only the clock: an interval that starts
 * before its bucket reads as a mistake without one, and the whole point of the
 * drill-down is that its timestamps can be checked against the printer's own
 * history.
 */
const SITE_INSTANT_FORMAT: Intl.DateTimeFormatOptions = {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
};

/** A naive-UTC instant as the SITE's wall clock, from the site's own offset. */
export function formatSiteInstant(
  naiveUtc: string,
  utcOffsetMinutes: number,
  locale: string,
  options: Intl.DateTimeFormatOptions = SITE_INSTANT_FORMAT,
): string {
  const shifted = siteInstant(naiveUtc, utcOffsetMinutes);
  if (Number.isNaN(shifted.getTime())) return naiveUtc;
  return new Intl.DateTimeFormat(locale, { ...options, timeZone: UTC }).format(shifted);
}

/** How a calendar DATE is spelled once the server has already resolved it. */
const SITE_DATE_FORMAT: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
};

/**
 * A site CALENDAR DATE (`YYYY-MM-DD`, already resolved by the server) spelled
 * the way the operator's locale spells one. Parsed AND formatted in UTC, so the
 * browser's own zone can never shift the server's date by a day.
 */
export function formatSiteDate(
  date: string,
  locale: string,
  options: Intl.DateTimeFormatOptions = SITE_DATE_FORMAT,
): string {
  const parsed = Date.parse(`${date}T00:00:00Z`);
  if (Number.isNaN(parsed)) return date;
  return new Intl.DateTimeFormat(locale, { ...options, timeZone: UTC }).format(new Date(parsed));
}

/**
 * The site calendar DAY an instant fell on, named by the site's ZONE.
 *
 * The third shape, and the reason there are three: the live `/status` response
 * carries `tz_name` but no offset, so an instant from it (`recording_since`)
 * cannot go through `formatSiteInstant`. The browser carries the IANA database,
 * so the zone name resolves the day exactly, with no second offset table. An
 * unrecognised zone makes `Intl` throw, so this falls back to UTC rather than
 * leaving a hole in the sentence.
 */
export function formatInstantSiteDay(naiveUtc: string, tzName: string, locale: string): string {
  const parsed = Date.parse(`${naiveUtc}Z`);
  if (Number.isNaN(parsed)) return naiveUtc;
  try {
    return new Intl.DateTimeFormat(locale, { ...SITE_DATE_FORMAT, timeZone: tzName }).format(parsed);
  } catch {
    return new Intl.DateTimeFormat(locale, { ...SITE_DATE_FORMAT, timeZone: UTC }).format(parsed);
  }
}

/**
 * The site's wall clock for a naive-UTC instant, as a sortable
 * `YYYY-MM-DDTHH:MM:SS`.
 *
 * The fourth shape, and the only one that is not a LABEL: comparing "does this
 * window start before the recorder did" means putting an instant beside a site
 * MIDNIGHT, and a formatted date cannot be compared with anything. `en-CA` is
 * chosen purely for its ISO part order — nothing this returns is ever shown to
 * a reader, and the operator's locale must not change the answer.
 *
 * Null on an unparseable instant; an unknown ZONE falls back to UTC, the same
 * way `formatInstantSiteDay` does, rather than refusing to answer at all.
 */
export function siteWallClock(naiveUtc: string, tzName: string): string | null {
  const parsed = Date.parse(`${naiveUtc}Z`);
  if (Number.isNaN(parsed)) return null;
  const read = (timeZone: string): string => {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(parsed);
    const at = (type: Intl.DateTimeFormatPartTypes): string =>
      parts.find((part) => part.type === type)?.value ?? '00';
    return `${at('year')}-${at('month')}-${at('day')}T${at('hour')}:${at('minute')}:${at('second')}`;
  };
  try {
    return read(tzName);
  } catch {
    return read(UTC);
  }
}

/**
 * Does the resolved window begin BEFORE the state recorder did?
 *
 * The question the Fleet tab's honesty notice is built on, and it is not "is
 * this a first run". On a real instance the recorder is hours old while the
 * print log and the fault ledger go back weeks, so nearly every window an
 * operator picks is mostly unrecorded — and a tab that only said so when
 * NOTHING had ever been observed said nothing at all in the case that matters.
 *
 * The window starts at midnight of `dateFrom` in the SITE's zone, so a window
 * beginning on the recorder's own start date still precedes it unless recording
 * began at exactly midnight. Both sides are the same sortable shape, so the
 * comparison is a string compare and no second offset table is involved.
 *
 * A recorder that has never written (`recording_since` null) precedes nothing —
 * but every window is then entirely unrecorded, which is the same thing to say.
 */
export function windowPrecedesRecording(
  dateFrom: string,
  recordingSince: string | null,
  tzName: string,
): boolean {
  if (recordingSince === null) return true;
  const wall = siteWallClock(recordingSince, tzName);
  // Unparseable: nothing is known, and a claim nobody can check is worse than
  // no claim.
  if (wall === null) return false;
  return `${dateFrom}T00:00:00` < wall;
}

/**
 * A window that starts and ends on the same site date names that date ONCE.
 * "Sep 21, 2026 – Sep 21, 2026" is a range whose two ends a reader has to
 * compare character by character to discover it is one day.
 */
export function isSingleDayRange(dateFrom: string, dateTo: string): boolean {
  return dateFrom === dateTo;
}

/** The summary header's leaf: one date or two, and with the bucket or without. */
export function headerRangeKey(singleDay: boolean, pending: boolean): string {
  if (pending) {
    return singleDay ? 'fleetMetrics.header.dayPending' : 'fleetMetrics.header.rangePending';
  }
  return singleDay ? 'fleetMetrics.header.day' : 'fleetMetrics.header.range';
}

/** The matrix caption's leaf, by the same rule. */
export function matrixCaptionKey(singleDay: boolean): string {
  return singleDay ? 'fleetMetrics.matrix.captionDay' : 'fleetMetrics.matrix.caption';
}

/** A span on the wire: a naive-UTC start and an end that may still be open. */
export interface OpenEndedSpan {
  start: string;
  /** Null while it is still running. */
  end: string | null;
}

/**
 * Does a span overlap a bucket's `[start, end)`?
 *
 * HALF-OPEN on both sides: a span that ends exactly when the bucket begins, or
 * begins exactly when it ends, does not overlap it. Touching is not overlapping,
 * and a closed-at-16:00 incident listed under the 16:00–17:00 hour is a fact
 * about the hour before.
 *
 * The drill-down asks its endpoint for whole site DAYS (that is the only grain
 * it has), so an hour bucket's answer routinely carries rows from the other
 * twenty-three — this is the predicate that keeps them out of the hour's list.
 * `openEndMs` closes a still-running span at the answer's own "now", so an open
 * incident overlaps every bucket up to it and none after.
 */
export function overlapsBucket(
  span: OpenEndedSpan,
  bucketStartMs: number,
  bucketEndMs: number,
  openEndMs: number,
): boolean {
  const start = Date.parse(`${span.start}Z`);
  const end = span.end === null ? openEndMs : Date.parse(`${span.end}Z`);
  if (Number.isNaN(start) || Number.isNaN(end)) return false;
  return end > bucketStartMs && start < bucketEndMs;
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

/**
 * Past this many, a chart TICK is spelled compactly.
 *
 * An axis column is measured in characters, not in pixels: `10,000` is six
 * glyphs where `10K` is three, and the axis has to reserve room for the widest
 * label it will draw. Four digits still read as a number at a glance, so the
 * threshold sits where the fifth one arrives.
 */
export const COMPACT_TICK_FROM = 10_000;

/**
 * A chart tick's own formatter — grouped below the threshold, compact above it.
 * Through `Intl`, so the compact suffix is the locale's own (`12 k`, `1.2万`).
 */
export function formatTickCount(value: number, locale: string): string {
  if (Math.abs(value) < COMPACT_TICK_FROM) return formatCount(value, locale);
  return new Intl.NumberFormat(locale, {
    notation: 'compact',
    maximumFractionDigits: 1,
  }).format(value);
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
  /**
   * The hour of the site day, 0–23, for an hour bucket; null otherwise.
   *
   * The NUMBER as well as the label, because the every-third-hour header rule
   * is arithmetic on the hour itself and must not be re-derived by parsing a
   * locale-formatted string — `hour` is spelled by `Intl` and a locale that
   * writes midnight as `24:00` would put the rule one column out.
   */
  hourOfDay: number | null;
  /** A week bucket's own start date. A CLIPPED first week keeps its real start. */
  weekStart: string | null;
  isWeekend: boolean;
  /** "now" falls inside this bucket: it has elapsed time but is not yet full. */
  isCurrent: boolean;
  /**
   * "Today" belongs on this bucket — the DAY bucket that contains now, and
   * only that.
   *
   * Not the same question as `isCurrent`, which is about a bucket's geometry at
   * every width. On an all-today HOUR grid the current bucket is one hour, so
   * the chip landed on a single 14 px column — labelling one hour of today
   * "Today" while the twenty-three beside it are the same day, and breaking the
   * every-third-hour cadence where it sat. On a WEEK grid the current week is
   * named by its own start date like every other week.
   */
  showsTodayChip: boolean;
  /** The full site-local date (and time, for an hour bucket) for the sr-only header. */
  full: string;
}

/**
 * TWO partialities, never one flag — they are different claims about different
 * things, and conflating them is how a chart ends up marking a complete print
 * count as unreliable, or a half-finished day as fully observed.
 *
 * - `inProgress` — the bucket has not FINISHED (`elapsed < seconds`, i.e.
 *   "today so far"). Every SUM over it is smaller than a full bucket's would
 *   be, whatever the sum is of: prints, incidents, SKU units, hours down. It
 *   says nothing about whether the data is trustworthy.
 * - `partlyObserved` — the state RECORDER covered less of the bucket than
 *   elapsed (beyond the sampler's tolerance), or covered none of it at all
 *   (`basis === 'incidents_only'`). Only STATE-derived figures are uncertain;
 *   prints, incidents and units come from ledgers that are complete for their
 *   own history, and marking those would be a lie in the safe direction.
 *
 * Which widget reads which is the widget's own decision, stated at its call
 * site — this module states the facts, not the policy.
 */
export type BucketUncertainty = 'inProgress' | 'partlyObserved';

/**
 * A chart whose figures come from a COMPLETE ledger — the print log, the
 * incident ledger, the queue's finished plates. Only a bucket that has not
 * finished makes its bars short; a recorder gap never hid a print.
 */
export const SUM_UNCERTAINTY: readonly BucketUncertainty[] = ['inProgress'];

/**
 * A chart whose figures are derived from the STATE recorder. Both partialities
 * apply: an unfinished bucket is short because the day is not over, and a
 * partly observed one is short because nobody was watching.
 */
export const STATE_SUM_UNCERTAINTY: readonly BucketUncertainty[] = [
  'inProgress',
  'partlyObserved',
];

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
  /** The bucket is still running: any SUM over it is short of a full one. */
  bucketInProgress: boolean;
  /** The recorder fell short of the bucket: STATE-derived sums are uncertain. */
  bucketPartlyObserved: boolean;
}

/** Does this row carry any of the uncertainties a given surface cares about? */
/**
 * Which points in a series have nothing to join to.
 *
 * A line is drawn BETWEEN neighbouring points, so a series with one non-null
 * bucket is a zero-length path: with `dot={false}` it paints nothing at all and
 * the legend then promises a line the chart never draws. That is the ordinary
 * state of every observed-only series on a young instance — "Prints per
 * printer" exists for observed buckets alone, and in the first days there is
 * exactly one of them.
 *
 * A point is isolated when BOTH its neighbours are absent; a run of two or more
 * has a segment to draw and stays a plain line. Off the ends counts as absent,
 * so a lone leading or trailing point is isolated too.
 */
export function isolatedPointFlags(values: readonly (number | null | undefined)[]): boolean[] {
  const missing = (value: number | null | undefined): boolean =>
    value === null || value === undefined;
  return values.map(
    (value, index) => !missing(value) && missing(values[index - 1]) && missing(values[index + 1]),
  );
}

/**
 * The isolated points of one series, keyed by the BUCKET they belong to.
 *
 * Keyed and not indexed, because a chart library is free to renumber: recharts
 * drops the null points before it renders marks (`computeLinePoints` ends in
 * `.filter(Boolean)`), so the index it hands a dot renderer counts only the
 * points that survived. A flags array indexed by ROW therefore asked the wrong
 * question of every series with a gap in it — which is every series this rule
 * exists for — and the mark silently never appeared.
 *
 * `bucketStart` is the row's own stable key, and it rides the payload recharts
 * gives back, so the answer does not depend on how anything is counted.
 */
export function isolatedPointKeys<Row extends { bucketStart: string }>(
  rows: readonly Row[],
  value: (row: Row) => number | null | undefined,
): Set<string> {
  const flags = isolatedPointFlags(rows.map(value));
  const keys = new Set<string>();
  rows.forEach((row, index) => {
    if (flags[index]) keys.add(row.bucketStart);
  });
  return keys;
}

export function rowIsUncertain(
  row: Pick<SeriesRowMeta, 'bucketInProgress' | 'bucketPartlyObserved'>,
  kinds: readonly BucketUncertainty[],
): boolean {
  return kinds.some((kind) =>
    kind === 'inProgress' ? row.bucketInProgress : row.bucketPartlyObserved,
  );
}

export type SeriesRow<Values extends object> = SeriesRowMeta & Values;

export interface SeriesRowsOptions {
  /** The bucket the server ECHOED — never the one the client asked for. */
  bucket: FleetBucket;
  /** The active i18n language, for the weekday and month names. */
  locale: string;
}

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
  const isCurrent = bucket.elapsed_seconds > 0 && bucket.elapsed_seconds < bucket.seconds;
  return {
    date,
    weekdayInitial: new Intl.DateTimeFormat(locale, { weekday: 'narrow', timeZone: UTC }).format(shifted),
    dayOfMonth: new Intl.DateTimeFormat(locale, { day: 'numeric', timeZone: UTC }).format(shifted),
    month: previousMonth === null || previousMonth !== month ? month : null,
    hour: isHour
      ? new Intl.DateTimeFormat(locale, {
          hour: '2-digit',
          minute: '2-digit',
          // `hourCycle: 'h23'`, never `hour12: false`. The two are not
          // synonyms: `hour12: false` selects the locale's own 24-hour cycle,
          // and for several locales ICU makes that **h24**, which spells
          // midnight `24:00`. So a bucket starting at the site's midnight read
          // "24:00" — an hour that does not exist, on the row above the day it
          // opens. `h23` is the 00–23 cycle by name and is locale-invariant;
          // `SITE_INSTANT_FORMAT` above has always used it.
          hourCycle: 'h23',
          timeZone: UTC,
        }).format(shifted)
      : null,
    hourOfDay: isHour ? shifted.getUTCHours() : null,
    weekStart: width === 'week' ? date : null,
    isWeekend: weekday === 0 || weekday === 6,
    isCurrent,
    // The chip names a DAY, so only the day grid carries one.
    showsTodayChip: isCurrent && width === 'day',
    full: new Intl.DateTimeFormat(locale, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      // Same cycle as `hour` above: the sr-only stamp on every hour column and
      // the bucket detail's own title are both built from this.
      ...(isHour ? { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' as const } : {}),
      timeZone: UTC,
    }).format(shifted),
  };
}

/**
 * How often an hour column gets a VISIBLE header.
 *
 * Every third, so the labels read 00 · 03 · 06. An hour column is 14 px wide
 * and a two-digit label is about 13 px, so labelling every one ran the hours
 * together into `000102030405…` — a band of digits that says nothing and hides
 * the day boundaries underneath it. Three is the least that separates them.
 */
export const HOUR_HEADER_LABEL_EVERY = 3;

/** Does this hour column carry a visible header? */
export function showsHourLabel(label: Pick<BucketLabel, 'hourOfDay'>): boolean {
  return label.hourOfDay !== null && label.hourOfDay % HOUR_HEADER_LABEL_EVERY === 0;
}

/**
 * The visible text of an hour column's header, or `''` where the grid is too
 * tight to carry one.
 *
 * Built from the hour NUMBER, never sliced off `label.hour`. Slicing took the
 * first two characters of a locale-formatted clock, which is two assumptions at
 * once: that the hour comes first (it does not in every locale) and that the
 * cycle runs 00–23 (it did not — see `bucketLabel`, where midnight could format
 * as `24:00`). `hourOfDay` is `getUTCHours()` on the site-shifted instant, so it
 * is 0–23 by construction and no formatter can move it.
 *
 * The DIGITS still go through the locale (`pad`), the way every other figure on
 * this tab does; only the value is ours.
 *
 * Only the VISIBLE label thins out: every header still spells its full
 * site-local date and time as sr-only text, so a screen-reader user hears all
 * twenty-four and nothing is disclosed behind a control.
 */
export function hourHeaderLabel(label: Pick<BucketLabel, 'hourOfDay'>, locale: string): string {
  if (!showsHourLabel(label) || label.hourOfDay === null) return '';
  return pad(label.hourOfDay, locale);
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
    // "Still running", read from the bucket's own geometry — a bucket with no
    // elapsed time at all has not started, which is not the same as running.
    bucketInProgress: bucket.elapsed_seconds > 0 && bucket.elapsed_seconds < bucket.seconds,
    // "The recorder fell short", read from the recorder's own coverage.
    // `incidents_only` is the backend's word for covering none of it, and both
    // are read so a response stating only one still answers correctly.
    bucketPartlyObserved:
      bucket.basis === 'incidents_only' ||
      bucket.observed_seconds < bucket.elapsed_seconds - OBSERVED_TOLERANCE_S,
    ...pick(bucket.values, bucket),
  }));
}
