/**
 * `utils/fleetMetrics` — the Fleet tab's encoding, checked by MEASUREMENT.
 *
 * Two things this file refuses to take on trust:
 *
 * - **Contrast.** Every heat step's ratio against its paired text colour is
 *   computed here with the WCAG 2.x relative-luminance formula. "It looks
 *   readable" is how a 3.9:1 tile ships; the farm's matrix is read at a glance
 *   on a shop-floor screen.
 * - **Label coverage.** Every class the classifier can emit is resolved to a
 *   key and that key is looked up in the real `en.ts`. A class whose label key
 *   points at nothing renders as the raw key on the surface, which is exactly
 *   the failure nobody notices until an operator asks what
 *   `down:fault:z_reference_lost` means.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en';
import type { PrinterIncidentKind } from '../../api/client';
import type { FleetBucket, MatrixCell, SeriesBucket, SeriesEnvelope } from '../../types/fleetMetrics';
import {
  allClassKeys,
  BUCKET_COLUMN_WIDTH_PX,
  causeLabelKey,
  cellAbsence,
  classLabelKey,
  CHART_STACK_GROUPS,
  DOWN_CAUSE_ORDER,
  FAULT_KIND_ORDER,
  FLEET_ABSENCE_COLOR,
  FLEET_ABSENCE_TEXT,
  FLEET_GROUP_COLOR,
  FLEET_GROUP_TEXT,
  FLEET_STATUS_TOKEN_HEX,
  foldTimeSplit,
  formatCount,
  formatDuration,
  formatHours,
  formatInstantSiteDay,
  formatPercent,
  formatPoints,
  formatPrinters,
  formatSiteDate,
  formatSiteInstant,
  groupLabelKey,
  incidentKindCause,
  incidentKindColor,
  incidentKindTextColor,
  OUTCOME_COLOR,
  OUTCOME_ORDER,
  OUTCOME_TEXT,
  SKU_BAND_COLORS,
  skuBandColor,
  skuBandText,
  SUMMARY_ROW_NOW_GROUP,
  HEAT_RAMP_HOURS_DOWN_DARK,
  HEAT_RAMP_HOURS_DOWN_LIGHT,
  HEAT_RAMP_PRINTS_DARK,
  HEAT_RAMP_PRINTS_LIGHT,
  hoursDownBand,
  isRowHidden,
  lensRamp,
  OBSERVED_TOLERANCE_S,
  parseClassKey,
  printsBand,
  SECONDARY_TEXT_CLASS,
  seriesRows,
  SUMMARY_ROW_LABEL_KEY,
  TIME_SPLIT_BAND_COLOR,
  TIME_SPLIT_BAND_TEXT,
  VALUE_LADDER,
  downCauseColor,
  downCauseTextColor,
} from '../../utils/fleetMetrics';

// ── WCAG plumbing (implemented here, not imported — the point is to measure) ──

function channel(value: number): number {
  const s = value / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const h = hex.replace('#', '');
  return (
    0.2126 * channel(parseInt(h.slice(0, 2), 16)) +
    0.7152 * channel(parseInt(h.slice(2, 4), 16)) +
    0.0722 * channel(parseInt(h.slice(4, 6), 16))
  );
}

function contrast(a: string, b: string): number {
  const la = luminance(a);
  const lb = luminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** Hue in degrees, for the "stayed in its token's family" pin. */
function hue(hex: string): number {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16) / 255;
  const g = parseInt(h.slice(2, 4), 16) / 255;
  const b = parseInt(h.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b);
  const delta = max - Math.min(r, g, b);
  if (delta === 0) return 0;
  const raw = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  const degrees = raw * 60;
  return degrees < 0 ? degrees + 360 : degrees;
}

const lookup = (dotted: string): unknown =>
  dotted.split('.').reduce<unknown>((node, part) => {
    if (node === null || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, en);

// ── class keys ──────────────────────────────────────────────────────────────

describe('parseClassKey', () => {
  it('splits every key in the vocabulary', () => {
    for (const key of allClassKeys()) {
      const parsed = parseClassKey(key);
      const rebuilt = parsed.cause === null ? parsed.group : `${parsed.group}:${parsed.cause}`;
      expect(rebuilt).toBe(key);
    }
  });

  it('carries the incident kind out of a fault key and only a fault key', () => {
    expect(parseClassKey('down:fault:jam')).toEqual({
      group: 'down',
      cause: 'fault:jam',
      faultKind: 'jam',
    });
    expect(parseClassKey('down:offline').faultKind).toBeNull();
    expect(parseClassKey('printing').faultKind).toBeNull();
  });

  it('refuses a group this build does not know', () => {
    expect(() => parseClassKey('sideways')).toThrow(RangeError);
    expect(() => parseClassKey('down:fault:')).toThrow(RangeError);
  });

  it('covers all eight groups and all twelve down causes', () => {
    const keys = allClassKeys();
    // printing + 2 cycle + idle + 6 conditions + 6 faults + planned + the three
    // absence groups.
    expect(keys).toHaveLength(20);
    expect(DOWN_CAUSE_ORDER).toHaveLength(12);
    // Actionable causes lead, so the first band a reader sees is a fixable one.
    expect(DOWN_CAUSE_ORDER.slice(0, 4)).toEqual([
      'plate_held',
      'no_usb',
      'paused',
      'model_mismatch',
    ]);
  });
});

// ── labels ──────────────────────────────────────────────────────────────────

describe('label keys', () => {
  it('resolves every class key to a leaf that exists in en.ts', () => {
    const missing = allClassKeys().filter((key) => typeof lookup(classLabelKey(key)) !== 'string');
    expect(missing).toEqual([]);
  });

  it('resolves every group and every down cause to a leaf that exists', () => {
    const groups = CHART_STACK_GROUPS.map(groupLabelKey);
    const causes = DOWN_CAUSE_ORDER.map(causeLabelKey);
    const missing = [...groups, ...causes].filter((key) => typeof lookup(key) !== 'string');
    expect(missing).toEqual([]);
  });

  it('reuses the printer surfaces rather than minting duplicate leaves', () => {
    expect(classLabelKey('printing')).toBe('printers.status.printing');
    expect(classLabelKey('idle')).toBe('printers.status.idle');
    expect(classLabelKey('down:offline')).toBe('printers.status.offline');
    expect(classLabelKey('down:paused')).toBe('printers.status.paused');
    expect(classLabelKey('down:quarantined')).toBe('printers.quarantine.badge');
    expect(classLabelKey('down:no_usb')).toBe('printers.noUsb');
    expect(classLabelKey('down:model_mismatch')).toBe('printers.modelMismatch.badge');
    expect(classLabelKey('down:plate_held')).toBe(
      'productionRuns.detail.eligibility.awaitingPlateClear',
    );
    expect(classLabelKey('down:fault:power_loss')).toBe('printers.incident.power_loss');
  });

  it('resolves every summary row to a leaf that exists', () => {
    const missing = Object.values(SUMMARY_ROW_LABEL_KEY).filter(
      (key) => typeof lookup(key) !== 'string',
    );
    expect(missing).toEqual([]);
  });
});

// ── palette ─────────────────────────────────────────────────────────────────

// ── the grounds, read from the stylesheet that actually ships ───────────────
//
// Parsed rather than re-typed: a ground added to `index.css` joins these checks
// on its own, which is the only way a palette constraint stays true.

const CSS = fs.readFileSync(
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../index.css'),
  'utf8',
);

/** Every `--bg-secondary` (the card ground, per `Dashboard`'s widget shell). */
function cardGrounds(): { light: Record<string, string>; dark: Record<string, string> } {
  const light: Record<string, string> = {};
  const dark: Record<string, string> = {};
  // Comments first: `/* Dark mode backgrounds */` sits immediately above
  // `.dark {`, and a selector capture that swallowed it would silently drop
  // that ground from every constraint below.
  const stripped = CSS.replace(/\/\*[\s\S]*?\*\//g, '');
  const blocks = stripped.matchAll(/(^|\})\s*([^{}]+?)\s*\{([^}]*)\}/gm);
  for (const block of blocks) {
    const selector = (block[2] ?? '').trim();
    const declared = /--bg-secondary:\s*(#[0-9a-fA-F]{6})/.exec(block[3] ?? '');
    if (!declared) continue;
    const hex = declared[1]!.toLowerCase();
    if (selector === ':root') light.neutral = hex;
    else if (/^\.dark(\.bg-([a-z]+))?$/.test(selector)) {
      dark[/\.bg-([a-z]+)/.exec(selector)?.[1] ?? 'neutral'] = hex;
    } else if (/^\.bg-([a-z]+)$/.test(selector)) {
      light[/\.bg-([a-z]+)/.exec(selector)![1]!] = hex;
    }
  }
  return { light, dark };
}

const GROUNDS = cardGrounds();

/** A tile must stand off its ground by at least this much to read as a tile. */
const GROUND_SEPARATION = 1.25;
/** Adjacent ramp steps must differ by at least this much to read as five steps. */
const STEP_SEPARATION = 1.15;

describe('theme grounds', () => {
  it('finds every ground the stylesheet declares', () => {
    // 3 light + 6 dark. If this count moves, a ground was added and the
    // palette constraints below now cover it — that is the point.
    expect(Object.keys(GROUNDS.light).sort()).toEqual(['cool', 'neutral', 'warm']);
    expect(Object.keys(GROUNDS.dark).sort()).toEqual([
      'cool',
      'forest',
      'neutral',
      'oled',
      'slate',
      'warm',
    ]);
  });
});

describe('palette', () => {
  const everyColour = [
    ...Object.values(FLEET_GROUP_COLOR),
    ...Object.values(FLEET_GROUP_TEXT),
    ...Object.values(FLEET_ABSENCE_COLOR),
    ...Object.values(FLEET_ABSENCE_TEXT),
    ...Object.values(TIME_SPLIT_BAND_COLOR),
    ...Object.values(TIME_SPLIT_BAND_TEXT),
    ...DOWN_CAUSE_ORDER.map(downCauseColor),
    ...DOWN_CAUSE_ORDER.map(downCauseTextColor),
    ...HEAT_RAMP_HOURS_DOWN_LIGHT.flatMap((step) => [step.background, step.text]),
    ...HEAT_RAMP_HOURS_DOWN_DARK.flatMap((step) => [step.background, step.text]),
    ...HEAT_RAMP_PRINTS_LIGHT.flatMap((step) => [step.background, step.text]),
    ...HEAT_RAMP_PRINTS_DARK.flatMap((step) => [step.background, step.text]),
  ];

  it('never encodes data with the user-selectable accent', () => {
    for (const colour of everyColour) {
      expect(colour).not.toContain('accent');
      expect(colour).not.toContain('bambu-green');
      expect(colour).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it('separates the five bands by a strictly monotonic value ladder', () => {
    const luminances = VALUE_LADDER.map((group) => luminance(FLEET_GROUP_COLOR[group]));
    for (let index = 1; index < luminances.length; index += 1) {
      expect(luminances[index]!).toBeLessThan(luminances[index - 1]!);
    }
    // Printing brightest, idle darkest — the stated rule, not an accident.
    expect(VALUE_LADDER[0]).toBe('printing');
    expect(VALUE_LADDER[VALUE_LADDER.length - 1]).toBe('idle');
  });

  it('keeps each band in its status token hue family', () => {
    expect(Math.abs(hue(FLEET_GROUP_COLOR.printing) - hue(FLEET_STATUS_TOKEN_HEX.ok))).toBeLessThan(15);
    expect(FLEET_GROUP_COLOR.down).toBe(FLEET_STATUS_TOKEN_HEX.error);
    expect(Math.abs(hue(FLEET_GROUP_COLOR.planned) - hue(FLEET_STATUS_TOKEN_HEX.warning))).toBeLessThan(15);
    // Idle is a true neutral: red, green and blue are equal.
    expect(FLEET_GROUP_COLOR.idle).toMatch(/^#([0-9a-f]{2})\1\1$/);
  });

  it('is still derived from the tokens index.css actually declares', () => {
    expect(CSS).toContain(`--status-ok: ${FLEET_STATUS_TOKEN_HEX.ok}`);
    expect(CSS).toContain(`--status-error: ${FLEET_STATUS_TOKEN_HEX.error}`);
    expect(CSS).toContain(`--status-warning: ${FLEET_STATUS_TOKEN_HEX.warning}`);
  });

  it('names the secondary text class that passes AA as body text', () => {
    expect(SECONDARY_TEXT_CLASS).toBe('text-bambu-gray-light');
  });

  it('gives each down cause its own shade, legibly paired and monotonic', () => {
    const shades = DOWN_CAUSE_ORDER.map(downCauseColor);
    expect(new Set(shades).size).toBe(shades.length);
    for (const cause of DOWN_CAUSE_ORDER) {
      expect(contrast(downCauseColor(cause), downCauseTextColor(cause))).toBeGreaterThanOrEqual(4.5);
    }
    const luminances = shades.map(luminance);
    for (let index = 1; index < luminances.length; index += 1) {
      expect(luminances[index]!).toBeLessThan(luminances[index - 1]!);
    }
  });

  it('pairs every band colour with a text colour that measures AA', () => {
    for (const group of CHART_STACK_GROUPS) {
      expect(contrast(FLEET_GROUP_COLOR[group], FLEET_GROUP_TEXT[group])).toBeGreaterThanOrEqual(4.5);
    }
    expect(
      contrast(FLEET_ABSENCE_COLOR.unobserved, FLEET_ABSENCE_TEXT.unobserved),
    ).toBeGreaterThanOrEqual(4.5);
    for (const band of ['printing', 'other', 'down'] as const) {
      expect(contrast(TIME_SPLIT_BAND_COLOR[band], TIME_SPLIT_BAND_TEXT[band])).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('pairs every OUTCOME band with a text colour that measures AA', () => {
    // Moved here out of `widgets/PrintsPerDayWidget`, where no test could
    // measure it. A text-bearing pairing that lives outside this module is a
    // pairing nobody checks.
    for (const outcome of OUTCOME_ORDER) {
      expect(contrast(OUTCOME_COLOR[outcome], OUTCOME_TEXT[outcome])).toBeGreaterThanOrEqual(4.5);
    }
    // Composed from the class palette, not re-typed as fresh hexes — so a hue
    // change lands in both places at once or in neither.
    expect(OUTCOME_COLOR.completed).toBe(FLEET_GROUP_COLOR.printing);
    expect(OUTCOME_COLOR.failed).toBe(FLEET_GROUP_COLOR.down);
  });

  it('pairs every SKU band with a text colour that measures AA, remainder included', () => {
    // Moved here out of `widgets/PartsBySkuWidget`.
    for (let index = 0; index < SKU_BAND_COLORS.length; index += 1) {
      expect(
        contrast(skuBandColor(index, false), skuBandText(index, false)),
      ).toBeGreaterThanOrEqual(4.5);
    }
    expect(contrast(skuBandColor(0, true), skuBandText(0, true))).toBeGreaterThanOrEqual(4.5);
    // A sixth SKU wraps rather than falling off the palette.
    expect(skuBandColor(SKU_BAND_COLORS.length, false)).toBe(skuBandColor(0, false));
  });

  it('pairs every INCIDENT KIND band with a text colour that measures AA', () => {
    // Moved here out of `widgets/RecoveryWidget`.
    const kinds: PrinterIncidentKind[] = [...FAULT_KIND_ORDER, 'service_hold'];
    for (const kind of kinds) {
      expect(
        contrast(incidentKindColor(kind), incidentKindTextColor(kind)),
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('gives a declared hold the maintenance hue, never a fault shade', () => {
    // A service window is planned work. Painting it red would report every
    // deliberate maintenance hour as a breakdown in the stacked chart.
    expect(incidentKindColor('service_hold')).toBe(FLEET_GROUP_COLOR.planned);
    expect(incidentKindColor('jam')).toBe(downCauseColor('fault:jam'));
    expect(incidentKindCause('jam')).toBe('fault:jam');
  });

  it('keeps every Time-split band and the no-data swatch off every ground', () => {
    const tiles = [...Object.values(TIME_SPLIT_BAND_COLOR), FLEET_ABSENCE_COLOR.unobserved];
    for (const ground of [...Object.values(GROUNDS.light), ...Object.values(GROUNDS.dark)]) {
      for (const tile of tiles) {
        expect(contrast(tile, ground)).toBeGreaterThanOrEqual(GROUND_SEPARATION);
      }
    }
  });
});

// ── heat ramps: one per resolved theme mode ─────────────────────────────────

describe('heat ramps', () => {
  const RAMPS = [
    { name: 'hours_down/light', ramp: HEAT_RAMP_HOURS_DOWN_LIGHT, mode: 'light' as const, rising: false },
    { name: 'hours_down/dark', ramp: HEAT_RAMP_HOURS_DOWN_DARK, mode: 'dark' as const, rising: true },
    { name: 'prints/light', ramp: HEAT_RAMP_PRINTS_LIGHT, mode: 'light' as const, rising: false },
    { name: 'prints/dark', ramp: HEAT_RAMP_PRINTS_DARK, mode: 'dark' as const, rising: true },
  ];

  it.each(RAMPS)('$name has five steps, each legible on its paired text', ({ ramp }) => {
    expect(ramp).toHaveLength(5);
    for (const step of ramp) {
      expect(contrast(step.background, step.text)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it.each(RAMPS)('$name puts emphasis on the HIGH value, not the low one', ({ ramp, rising }) => {
    const luminances = ramp.map((step) => luminance(step.background));
    for (let index = 1; index < luminances.length; index += 1) {
      if (rising) expect(luminances[index]!).toBeGreaterThan(luminances[index - 1]!);
      else expect(luminances[index]!).toBeLessThan(luminances[index - 1]!);
    }
  });

  it.each(RAMPS)('$name separates adjacent steps so it reads as five, not three', ({ ramp }) => {
    for (let index = 1; index < ramp.length; index += 1) {
      expect(contrast(ramp[index]!.background, ramp[index - 1]!.background)).toBeGreaterThanOrEqual(
        STEP_SEPARATION,
      );
    }
  });

  it.each(RAMPS)('$name stands step 1 off every ground of its own mode', ({ ramp, mode }) => {
    const grounds = mode === 'dark' ? GROUNDS.dark : GROUNDS.light;
    expect(Object.keys(grounds).length).toBeGreaterThanOrEqual(3);
    for (const [name, ground] of Object.entries(grounds)) {
      const measured = contrast(ramp[1]!.background, ground);
      expect(
        measured,
        `${mode} ground "${name}" (${ground}) vs step 1 ${ramp[1]!.background}`,
      ).toBeGreaterThanOrEqual(GROUND_SEPARATION);
    }
  });

  it('ends both modes of a lens on the same saturated cap', () => {
    expect(HEAT_RAMP_HOURS_DOWN_DARK[4]!.background).toBe(HEAT_RAMP_HOURS_DOWN_LIGHT[4]!.background);
    expect(HEAT_RAMP_PRINTS_DARK[4]!.background).toBe(HEAT_RAMP_PRINTS_LIGHT[4]!.background);
    expect(HEAT_RAMP_HOURS_DOWN_DARK[4]!.text).toBe('#ffffff');
    expect(HEAT_RAMP_PRINTS_DARK[4]!.text).toBe('#ffffff');
  });

  it('uses white text throughout the dark ramps — measured, not assumed', () => {
    for (const ramp of [HEAT_RAMP_HOURS_DOWN_DARK, HEAT_RAMP_PRINTS_DARK]) {
      for (const step of ramp) {
        expect(step.text).toBe('#ffffff');
        expect(contrast(step.background, '#ffffff')).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe('lensRamp', () => {
  it('requires the resolved mode and returns that mode’s ramp', () => {
    expect(lensRamp('hours_down', 'light')).toBe(HEAT_RAMP_HOURS_DOWN_LIGHT);
    expect(lensRamp('hours_down', 'dark')).toBe(HEAT_RAMP_HOURS_DOWN_DARK);
    expect(lensRamp('prints', 'light')).toBe(HEAT_RAMP_PRINTS_LIGHT);
    expect(lensRamp('prints', 'dark')).toBe(HEAT_RAMP_PRINTS_DARK);
  });

  it('gives the two modes genuinely different ramps below the cap', () => {
    for (const lens of ['hours_down', 'prints'] as const) {
      const light = lensRamp(lens, 'light');
      const dark = lensRamp(lens, 'dark');
      for (let index = 0; index < 4; index += 1) {
        expect(dark[index]!.background).not.toBe(light[index]!.background);
      }
    }
  });
});

// ── bands ───────────────────────────────────────────────────────────────────

describe('hoursDownBand', () => {
  it('bands a day bucket at 0 / <1 / 1–4 / 4–12 / 12+', () => {
    expect(hoursDownBand(0, 'day')).toBe(0);
    expect(hoursDownBand(0.01, 'day')).toBe(1);
    expect(hoursDownBand(0.999, 'day')).toBe(1);
    expect(hoursDownBand(1, 'day')).toBe(2);
    expect(hoursDownBand(3.999, 'day')).toBe(2);
    expect(hoursDownBand(4, 'day')).toBe(3);
    expect(hoursDownBand(11.999, 'day')).toBe(3);
    expect(hoursDownBand(12, 'day')).toBe(4);
    expect(hoursDownBand(400, 'day')).toBe(4);
  });

  it('scales the same edges by seven for a week bucket', () => {
    expect(hoursDownBand(6.9, 'week')).toBe(1);
    expect(hoursDownBand(7, 'week')).toBe(2);
    expect(hoursDownBand(28, 'week')).toBe(3);
    expect(hoursDownBand(84, 'week')).toBe(4);
  });

  it('scales them down to a fraction of an hour for an hour bucket', () => {
    expect(hoursDownBand(1 / 24 - 0.001, 'hour')).toBe(1);
    expect(hoursDownBand(1 / 24, 'hour')).toBe(2);
    expect(hoursDownBand(4 / 24, 'hour')).toBe(3);
    expect(hoursDownBand(12 / 24, 'hour')).toBe(4);
  });

  it('never bands a negative or non-finite value above zero', () => {
    expect(hoursDownBand(-1, 'day')).toBe(0);
    expect(hoursDownBand(Number.NaN, 'day')).toBe(0);
  });
});

describe('printsBand', () => {
  it('scales relative to the window maximum in four steps', () => {
    expect(printsBand(0, 20)).toBe(0);
    expect(printsBand(1, 20)).toBe(1);
    expect(printsBand(5, 20)).toBe(1);
    expect(printsBand(6, 20)).toBe(2);
    expect(printsBand(15, 20)).toBe(3);
    expect(printsBand(20, 20)).toBe(4);
  });

  it('answers zero when the window has no maximum to scale against', () => {
    expect(printsBand(4, 0)).toBe(0);
  });
});

describe('column width follows the echoed bucket', () => {
  it('is 14 / 28 / 40 px', () => {
    const widths: Record<FleetBucket, number> = BUCKET_COLUMN_WIDTH_PX;
    expect(widths).toEqual({ hour: 14, day: 28, week: 40 });
  });
});

// ── time split ──────────────────────────────────────────────────────────────

describe('foldTimeSplit', () => {
  it('folds every class into exactly one band and sums to the elapsed seconds', () => {
    const elapsed = 86_400;
    const split = foldTimeSplit({
      printing: 40_000,
      'cycle_overhead:cooling': 4_000,
      'cycle_overhead:ejecting': 400,
      idle: 20_000,
      planned: 2_000,
      'down:fault:jam': 15_000,
      'down:plate_held': 3_000,
      unobserved: 2_000,
    });
    expect(split.printing).toBe(40_000);
    expect(split.other).toBe(26_400);
    expect(split.down).toBe(18_000);
    expect(split.noData).toBe(2_000);
    expect(split.total).toBe(elapsed);
    expect(split.printing + split.other + split.down + split.noData).toBe(split.total);
  });

  it('counts out-of-fleet and before-recording as absence, not as other', () => {
    const split = foldTimeSplit({ out_of_fleet: 10, not_recorded: 20, idle: 30 });
    expect(split.noData).toBe(30);
    expect(split.other).toBe(30);
  });
});

// ── absence ─────────────────────────────────────────────────────────────────

const header = (elapsed: number, observed: number): Pick<SeriesBucket<unknown>, 'elapsed_seconds' | 'observed_seconds'> => ({
  elapsed_seconds: elapsed,
  observed_seconds: observed,
});

const cell = (over: Partial<MatrixCell> = {}): MatrixCell => ({
  class_seconds: { idle: 86_400 },
  down_seconds: 0,
  prints: {},
  basis: 'observed',
  ...over,
});

describe('cellAbsence', () => {
  it('reads a genuinely zero cell as zero', () => {
    expect(cellAbsence(cell(), header(86_400, 86_400), { lens: 'hours_down' })).toBe('zero');
    expect(cellAbsence(cell(), header(86_400, 86_400), { lens: 'prints' })).toBe('zero');
  });

  it('reads a cell with a value as no absence at all', () => {
    const busy = cell({ down_seconds: 3600, class_seconds: { 'down:offline': 3600, idle: 82_800 }, prints: { completed: 4 } });
    expect(cellAbsence(busy, header(86_400, 86_400), { lens: 'hours_down' })).toBeNull();
    expect(cellAbsence(busy, header(86_400, 86_400), { lens: 'prints' })).toBeNull();
  });

  it('reads a window before this printer was ever recorded as before_recording', () => {
    const early = cell({ class_seconds: { not_recorded: 86_400 }, basis: 'incidents_only' });
    expect(cellAbsence(early, header(86_400, 0), { lens: 'hours_down' })).toBe('before_recording');
  });

  it('reads a deactivated printer as out_of_fleet under every lens', () => {
    const gone = cell({ class_seconds: { out_of_fleet: 86_400 } });
    expect(cellAbsence(gone, header(86_400, 86_400), { lens: 'hours_down' })).toBe('out_of_fleet');
    expect(cellAbsence(gone, header(86_400, 86_400), { lens: 'prints' })).toBe('out_of_fleet');
    expect(cellAbsence(gone, header(86_400, 86_400), { lens: 'time_split' })).toBe('out_of_fleet');
  });

  it('hatches a ledger-only cell and a partly observed bucket', () => {
    const ledgerOnly = cell({
      class_seconds: { 'down:fault:jam': 14_400, unobserved: 72_000 },
      down_seconds: 14_400,
      basis: 'incidents_only',
    });
    expect(cellAbsence(ledgerOnly, header(86_400, 86_400), { lens: 'hours_down' })).toBe('partial');

    const observedCell = cell({ down_seconds: 600, class_seconds: { 'down:offline': 600, idle: 85_800 } });
    expect(cellAbsence(observedCell, header(86_400, 43_200), { lens: 'hours_down' })).toBe('partial');
  });

  it('never hatches the Prints lens — the print log is complete for its own history', () => {
    const ledgerOnly = cell({
      class_seconds: { 'down:fault:jam': 14_400, unobserved: 72_000 },
      down_seconds: 14_400,
      basis: 'incidents_only',
      prints: { completed: 7 },
    });
    expect(cellAbsence(ledgerOnly, header(86_400, 0), { lens: 'prints' })).toBeNull();
    expect(cellAbsence(ledgerOnly, header(86_400, 43_200), { lens: 'prints' })).toBeNull();
  });

  it('tolerates a recorder that is a few samples short of the bucket', () => {
    const fine = cell({ down_seconds: 60, class_seconds: { 'down:offline': 60, idle: 86_340 } });
    expect(
      cellAbsence(fine, header(86_400, 86_400 - OBSERVED_TOLERANCE_S + 1), { lens: 'hours_down' }),
    ).toBeNull();
  });
});

describe('isRowHidden', () => {
  const outOfFleet = cell({ class_seconds: { out_of_fleet: 86_400 } });
  const inFleet = cell({ class_seconds: { idle: 86_400 } });

  it('hides a deleted printer that was out of the fleet for the whole window', () => {
    expect(isRowHidden({ deleted: true }, [outOfFleet, outOfFleet])).toBe(true);
  });

  it('keeps a deleted printer that was in the fleet for part of the window', () => {
    expect(isRowHidden({ deleted: true }, [inFleet, outOfFleet])).toBe(false);
  });

  it('never hides a printer that still exists', () => {
    expect(isRowHidden({ deleted: false }, [outOfFleet, outOfFleet])).toBe(false);
  });
});

// ── formatters ──────────────────────────────────────────────────────────────

describe('formatters', () => {
  it('gives hours one decimal below ten and none above', () => {
    expect(formatHours(4, 'en')).toBe('4.0');
    expect(formatHours(9.94, 'en')).toBe('9.9');
    expect(formatHours(31.4, 'en')).toBe('31');
    expect(formatHours(0, 'en')).toBe('0.0');
  });

  it('follows the active locale', () => {
    expect(formatHours(4.2, 'de')).toBe('4,2');
    expect(formatCount(1234, 'de')).toBe('1.234');
  });

  it('gives average printers one decimal', () => {
    expect(formatPrinters(2, 'en')).toBe('2.0');
    expect(formatPrinters(2.14, 'en')).toBe('2.1');
  });

  it('gives a ratio an integer percentage and a change in points', () => {
    expect(formatPercent(0.912, 'en')).toBe('91%');
    expect(formatPoints(-0.023, 'en')).toBe('2');
  });

  it('formats durations the way a card reads them', () => {
    expect(formatDuration(4 * 3600 + 2 * 60, 'en')).toBe('4 h 02 m');
    expect(formatDuration(2 * 86_400 + 4 * 3600, 'en')).toBe('2 d 4 h');
    expect(formatDuration(85, 'en')).toBe('1 m 25 s');
    expect(formatDuration(42, 'en')).toBe('42 s');
    expect(formatDuration(-5, 'en')).toBe('0 s');
  });
});

/**
 * The three site-time shapes, consolidated here out of four private copies
 * (`BucketDetail`, `FleetSummaryCard`, `FleetTab`, `FleetMatrix`). The rule
 * every one of them has to keep is the same: the browser's zone never enters
 * the answer.
 */
describe('site time', () => {
  const original = process.env.TZ;
  afterEach(() => {
    process.env.TZ = original;
  });

  const under = <T,>(tz: string, read: () => T): T => {
    process.env.TZ = tz;
    return read();
  };

  it('reads an instant on the SITE wall clock from the site offset', () => {
    // 2026-09-18T19:03:25 UTC at UTC+12 is 2026-09-19 07:03 site-local.
    expect(formatSiteInstant('2026-09-18T19:03:25', 720, 'en-GB')).toMatch(/19 Sept.*07:03/);
  });

  it('gives the same instant the same label fourteen hours either side of UTC', () => {
    const read = () => formatSiteInstant('2026-09-18T19:03:25', 720, 'en-GB');
    expect(under('Pacific/Kiritimati', read)).toBe(under('America/Los_Angeles', read));
  });

  it('never shifts a server-resolved calendar date by a day', () => {
    const read = () => formatSiteDate('2026-09-21', 'en-GB');
    expect(under('America/Los_Angeles', read)).toMatch(/21 Sept 2026/);
    expect(under('Pacific/Kiritimati', read)).toBe(under('America/Los_Angeles', read));
  });

  it('names the site DAY of an instant from the zone name alone', () => {
    // 2026-09-01T03:00 UTC is already 2026-09-01 15:00 in Auckland, and still
    // 2026-08-31 in Los Angeles — the zone decides, never the browser.
    const read = () => formatInstantSiteDay('2026-09-01T03:00:00', 'Pacific/Auckland', 'en-GB');
    expect(under('America/Los_Angeles', read)).toMatch(/1 Sept 2026/);
    expect(
      formatInstantSiteDay('2026-09-01T03:00:00', 'America/Los_Angeles', 'en-GB'),
    ).toMatch(/31 Aug 2026/);
  });

  it('falls back to UTC rather than leaving a hole when the zone is unknown', () => {
    expect(formatInstantSiteDay('2026-09-01T03:00:00', 'Mars/Olympus', 'en-GB')).toMatch(
      /1 Sept 2026/,
    );
  });

  it('hands back the raw value rather than "Invalid Date" on unparseable input', () => {
    expect(formatSiteDate('not-a-date', 'en-GB')).toBe('not-a-date');
    expect(formatSiteInstant('not-a-date', 0, 'en-GB')).toBe('not-a-date');
    expect(formatInstantSiteDay('not-a-date', 'UTC', 'en-GB')).toBe('not-a-date');
  });
});

describe('SUMMARY_ROW_NOW_GROUP', () => {
  it('pairs exactly the state rows with a live group, and no rate row', () => {
    expect(Object.keys(SUMMARY_ROW_NOW_GROUP).sort()).toEqual([
      'avg_cycle_overhead',
      'avg_down',
      'avg_idle',
      'avg_planned',
      'avg_printing',
    ]);
    // A rate has no instantaneous twin — "148 prints per day" is not a number
    // of printers, and pairing it with one would put two different units in
    // the same row.
    expect(SUMMARY_ROW_NOW_GROUP.prints_per_day).toBeUndefined();
    expect(SUMMARY_ROW_NOW_GROUP.uptime).toBeUndefined();
  });

  it('names a group the summary row label agrees with', () => {
    for (const key of Object.keys(SUMMARY_ROW_NOW_GROUP)) {
      expect(lookup(SUMMARY_ROW_LABEL_KEY[key as keyof typeof SUMMARY_ROW_LABEL_KEY])).toBeTypeOf(
        'string',
      );
    }
  });
});

describe('readChange', () => {
  it('reads direction from the DISPLAYED precision, not the raw float', async () => {
    const { readChange } = await import('../../utils/fleetMetrics');
    expect(readChange(7.61, 7.6, { precision: 1 })).toEqual({
      direction: 'none',
      magnitude: 0,
      labelKey: 'fleetMetrics.change.none',
    });
    const up = readChange(2.5, 2.1, { precision: 1 });
    expect(up?.direction).toBe('up');
    expect(up?.magnitude).toBeCloseTo(0.4, 6);
    expect(readChange(2.1, 2.5, { precision: 1 })?.direction).toBe('down');
  });

  it('answers null when either side is missing', async () => {
    const { readChange } = await import('../../utils/fleetMetrics');
    expect(readChange(null, 2)).toBeNull();
    expect(readChange(2, null)).toBeNull();
  });

  it('resolves each direction to a leaf that exists in en.ts', async () => {
    const { readChange } = await import('../../utils/fleetMetrics');
    for (const pair of [
      [2, 1],
      [1, 2],
      [1, 1],
    ] as const) {
      const reading = readChange(pair[0], pair[1]);
      expect(typeof lookup(reading!.labelKey)).toBe('string');
    }
  });
});

// ── the series adapter ──────────────────────────────────────────────────────

/** A site zone of UTC+12: a site day starts at 12:00 UTC the day before. */
const OFFSET = 720;

function bucket(start: string, over: Partial<SeriesBucket<{ value: number }>> = {}): SeriesBucket<{ value: number }> {
  return {
    start,
    seconds: 86_400,
    elapsed_seconds: 86_400,
    observed_seconds: 86_400,
    utc_offset_minutes: OFFSET,
    basis: 'observed',
    values: { value: 1 },
    ...over,
  };
}

const envelope = (buckets: SeriesBucket<{ value: number }>[]): SeriesEnvelope<{ value: number }> => ({
  buckets,
  totals: { value: buckets.length },
});

describe('seriesRows', () => {
  it('labels each bucket in the SITE zone, not the browser zone', () => {
    // 2026-09-30T12:00Z is 2026-10-01 00:00 at UTC+12 — a different calendar day.
    const rows = seriesRows(
      envelope([bucket('2026-09-30T12:00:00')]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows[0]!.bucketLabel.date).toBe('2026-10-01');
    expect(rows[0]!.bucketLabel.dayOfMonth).toBe('1');
  });

  it('names the month on the first bucket and at each change', () => {
    const rows = seriesRows(
      envelope([
        bucket('2026-09-28T12:00:00'),
        bucket('2026-09-29T12:00:00'),
        bucket('2026-09-30T12:00:00'),
      ]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    // The RULE, not a CLDR spelling: the platform's own short month for each
    // date, and null in between. ("Sep" vs "Sept" is ICU's business, not ours.)
    const shortMonth = (iso: string): string =>
      new Intl.DateTimeFormat('en-GB', { month: 'short', timeZone: 'UTC' }).format(new Date(iso));
    const months = rows.map((row) => row.bucketLabel.month);
    expect(months[0]).toBe(shortMonth('2026-09-29T00:00:00Z'));
    expect(months[1]).toBeNull();
    expect(months[2]).toBe(shortMonth('2026-10-01T00:00:00Z'));
    expect(months[0]).not.toBe(months[2]);
  });

  it('labels a week bucket by its own start, so a clipped first week keeps its real date', () => {
    // A window that begins mid-week: the first bucket is a Thursday, not a Monday.
    const rows = seriesRows(
      envelope([
        bucket('2026-09-02T12:00:00', { seconds: 345_600, elapsed_seconds: 345_600, observed_seconds: 345_600 }),
        bucket('2026-09-06T12:00:00', { seconds: 604_800, elapsed_seconds: 604_800, observed_seconds: 604_800 }),
      ]),
      (values) => values,
      { bucket: 'week', locale: 'en-GB' },
    );
    expect(rows[0]!.bucketLabel.weekStart).toBe('2026-09-03');
    expect(rows[1]!.bucketLabel.weekStart).toBe('2026-09-07');
  });

  it('survives an offset change mid-window (a DST transition)', () => {
    // NZ DST 2026: +720 before 2026-09-27, +780 after. Both site days read 00:00.
    const rows = seriesRows(
      envelope([
        bucket('2026-09-25T12:00:00', { utc_offset_minutes: 720 }),
        bucket('2026-09-26T12:00:00', { utc_offset_minutes: 720, seconds: 82_800, elapsed_seconds: 82_800, observed_seconds: 82_800 }),
        bucket('2026-09-27T11:00:00', { utc_offset_minutes: 780 }),
      ]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows.map((row) => row.bucketLabel.date)).toEqual(['2026-09-26', '2026-09-27', '2026-09-28']);
  });

  it('marks the bucket that is still running as current', () => {
    const rows = seriesRows(
      envelope([
        bucket('2026-09-19T12:00:00'),
        bucket('2026-09-20T12:00:00', { elapsed_seconds: 43_200 }),
        bucket('2026-09-21T12:00:00', { elapsed_seconds: 0, observed_seconds: 0 }),
      ]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows.map((row) => row.bucketLabel.isCurrent)).toEqual([false, true, false]);
  });

  it('flags a weekend and a partly observed bucket', () => {
    const rows = seriesRows(
      // 2026-09-18T12:00Z = Saturday 2026-09-19 at UTC+12.
      envelope([bucket('2026-09-18T12:00:00', { observed_seconds: 40_000 })]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows[0]!.bucketLabel.isWeekend).toBe(true);
    expect(rows[0]!.bucketPartlyObserved).toBe(true);
    // Finished, and short only because nobody was watching. The two flags are
    // separate precisely so this bucket is not also called "in progress".
    expect(rows[0]!.bucketInProgress).toBe(false);
  });

  it('separates a bucket that is still RUNNING from one that is under-observed', () => {
    const rows = seriesRows(
      envelope([
        // Finished and fully observed.
        bucket('2026-09-18T12:00:00'),
        // Finished, half observed: a recorder gap.
        bucket('2026-09-19T12:00:00', { observed_seconds: 40_000 }),
        // Half elapsed and observed all of it: today so far.
        bucket('2026-09-20T12:00:00', { elapsed_seconds: 43_200, observed_seconds: 43_200 }),
        // Elapsed but covered only by the incident ledger.
        bucket('2026-09-21T12:00:00', { observed_seconds: 0, basis: 'incidents_only' }),
      ]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );

    expect(rows.map((row) => row.bucketInProgress)).toEqual([false, false, true, false]);
    expect(rows.map((row) => row.bucketPartlyObserved)).toEqual([false, true, false, true]);
  });

  it('tolerates a recorder a couple of samples short of a finished bucket', () => {
    const rows = seriesRows(
      envelope([bucket('2026-09-18T12:00:00', { observed_seconds: 86_400 - 60 })]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows[0]!.bucketPartlyObserved).toBe(false);
  });

  it('calls a bucket with no elapsed time neither running nor short', () => {
    // A bucket that has not STARTED is a different thing from one part way
    // through: nothing is accumulating in it, so nothing about it understates.
    const rows = seriesRows(
      envelope([bucket('2026-09-22T12:00:00', { elapsed_seconds: 0, observed_seconds: 0 })]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows[0]!.bucketInProgress).toBe(false);
    expect(rows[0]!.bucketPartlyObserved).toBe(false);
  });

  it('spreads the picked values into a flat recharts row and keeps the header', () => {
    const rows = seriesRows(
      envelope([bucket('2026-09-18T12:00:00')]),
      (values, source) => ({ value: values.value, width: source.seconds }),
      { bucket: 'day', locale: 'en-GB' },
    );
    expect(rows[0]).toMatchObject({
      bucketStart: '2026-09-18T12:00:00',
      bucketIndex: 0,
      bucketSeconds: 86_400,
      bucketBasis: 'observed',
      value: 1,
      width: 86_400,
    });
  });
});

describe('site-local labels do not depend on the browser timezone', () => {
  const original = process.env.TZ;
  afterEach(() => {
    process.env.TZ = original;
  });

  const labelsUnder = (tz: string): string[] => {
    process.env.TZ = tz;
    return seriesRows(
      envelope([
        bucket('2026-09-30T12:00:00'),
        bucket('2026-10-01T12:00:00'),
        bucket('2026-10-02T12:00:00'),
      ]),
      (values) => values,
      { bucket: 'day', locale: 'en-GB' },
    ).map((row) => `${row.bucketLabel.date}/${row.bucketLabel.weekdayInitial}/${row.bucketLabel.month ?? '-'}`);
  };

  it('reads identically fourteen hours either side of UTC', () => {
    const kiritimati = labelsUnder('Pacific/Kiritimati');
    const losAngeles = labelsUnder('America/Los_Angeles');
    expect(kiritimati).toEqual(losAngeles);
    // And they are the SITE's dates, not either browser's.
    expect(kiritimati.map((label) => label.split('/')[0])).toEqual([
      '2026-10-01',
      '2026-10-02',
      '2026-10-03',
    ]);
    expect(kiritimati[0]).toMatch(/^2026-10-01\/T\//);
  });
});
