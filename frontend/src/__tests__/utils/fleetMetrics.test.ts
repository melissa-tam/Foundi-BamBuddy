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
  bucketHasElapsed,
  bucketLabel,
  BUCKET_COLUMN_WIDTH_PX,
  BUCKET_HOURS,
  currentBucketIndex,
  causeLabelKey,
  cellAbsence,
  classLabelKey,
  completedPrints,
  CHART_STACK_GROUPS,
  DOWN_CAUSE_ORDER,
  FAULT_KIND_ORDER,
  FLEET_ABSENCE_COLOR,
  FLEET_ABSENCE_TEXT,
  FLEET_AVG_DESCRIPTION_KEY,
  fleetAverage,
  FLEET_GROUP_COLOR,
  FLEET_GROUP_TEXT,
  FLEET_PARTIAL_BAND_PX,
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
  formatTickCount,
  groupLabelKey,
  headerRangeKey,
  hourHeaderLabel,
  isolatedPointFlags,
  isPartlyObserved,
  isSingleDayRange,
  matrixCaptionKey,
  overlapsBucket,
  partialMarkerCss,
  showsHourLabel,
  siteWallClock,
  summaryHintKeys,
  SUMMARY_ROW_UNIT_KEY,
  windowPrecedesRecording,
  incidentKindCause,
  incidentKindColor,
  incidentKindLabelKey,
  incidentKindTextColor,
  CELL_ABSENCE_LABEL_KEY,
  LENS_FLEET_AVG_MODE,
  LENS_LABEL_KEY,
  OUTCOME_LABEL_KEY,
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
  lensValue,
  lensRamp,
  OBSERVED_TOLERANCE_S,
  parseClassKey,
  printsBand,
  SECONDARY_TEXT_CLASS,
  seriesRows,
  sumMap,
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

/** The app's own source root — the scans below walk it. */
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

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

  /**
   * EVERY label key the Fleet tab can ask for, against the real `en.ts`.
   *
   * The defect this exists for: `incidentKindLabelKey` used to be spelled
   * `causeLabelKey(incidentKindCause(kind))` at four call sites, which builds
   * `fault:service_hold` for the one DECLARED kind and so asked for
   * `printers.incident.service_hold` — a leaf that does not exist. The Recovery
   * legend, its series name and its data-table header all printed that raw key
   * to the operator. The per-family tests above each passed, because none of
   * them walked the incident kinds.
   *
   * So this one walks every family the tab can name, in one place, and a new
   * member of any of them joins it automatically.
   */
  it('resolves EVERY label the Fleet tab can produce to a leaf that exists', () => {
    const kinds: PrinterIncidentKind[] = [
      'jam',
      'runout',
      'physical',
      'power_loss',
      'plate_vision',
      'z_reference_lost',
      // The declared kind — the one the old spelling got wrong.
      'service_hold',
    ];
    const keys = [
      ...allClassKeys().map(classLabelKey),
      ...CHART_STACK_GROUPS.map(groupLabelKey),
      ...DOWN_CAUSE_ORDER.map(causeLabelKey),
      ...kinds.map(incidentKindLabelKey),
      ...OUTCOME_ORDER.map((outcome) => OUTCOME_LABEL_KEY[outcome]),
      ...Object.values(SUMMARY_ROW_LABEL_KEY),
      ...Object.values(LENS_LABEL_KEY),
      ...Object.values(CELL_ABSENCE_LABEL_KEY),
    ];
    const missing = keys.filter((key) => typeof lookup(key) !== 'string');
    expect(missing).toEqual([]);
  });

  it('lets nobody but the util BUILD a printers.incident key', () => {
    // The class, not the instance. `printers.incident.service_hold` reached the
    // Incidents list as visible text because the dialog spelled the key with a
    // template — and the declared kind has no leaf there, by design. Any file
    // that assembles one is one `service_hold` away from the same bug, so the
    // rule is that only `incidentKindLabelKey` and `causeLabelKey` assemble it.
    const ALLOWED = new Set([
      // THE owner: `causeLabelKey` builds it and `incidentKindLabelKey` guards
      // the declared kind away from it.
      path.join('utils', 'fleetMetrics.ts'),
      // The printer card's own chip, whose kinds are all faults and which is
      // pinned by `__tests__/i18n/incidentKinds.test.ts`.
      path.join('pages', 'PrintersPage.tsx'),
    ]);
    const builder = /printers\.incident\.(?:\$\{|['"`]\s*\+)|['"`]printers\.incident\.['"`]\s*\+/;

    const offenders: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
          walk(full);
        } else if (/\.tsx?$/.test(entry.name)) {
          const relative = path.relative(SRC, full);
          if (ALLOWED.has(relative)) continue;
          if (builder.test(fs.readFileSync(full, 'utf8'))) offenders.push(relative);
        }
      }
    };
    walk(SRC);

    expect(offenders).toEqual([]);
  });

  it('names the declared hold as maintenance, never as a fault kind', () => {
    // `fault:service_hold` is not a cause the classifier can emit, so asking
    // for its label is asking for a key nobody wrote.
    expect(incidentKindLabelKey('service_hold')).toBe(groupLabelKey('planned'));
    expect(incidentKindLabelKey('jam')).toBe(causeLabelKey('fault:jam'));
    expect(lookup(incidentKindLabelKey('service_hold'))).toBe('Maintenance');
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

  it('tells a ledger-only cell from a partly observed one, and names each', () => {
    // They were ONE verdict (`partial`) and the cell named itself from the
    // bucket's `basis` instead — so `observed` resolved to the "No data" leaf
    // and a hatched cell showing a real 12 announced "No data" beside its own
    // figure. Two claims, two verdicts, two names.
    const ledgerOnly = cell({
      class_seconds: { 'down:fault:jam': 14_400, unobserved: 72_000 },
      down_seconds: 14_400,
      basis: 'incidents_only',
    });
    expect(cellAbsence(ledgerOnly, header(86_400, 86_400), { lens: 'hours_down' })).toBe(
      'incidents_only',
    );

    const observedCell = cell({ down_seconds: 600, class_seconds: { 'down:offline': 600, idle: 85_800 } });
    expect(cellAbsence(observedCell, header(86_400, 43_200), { lens: 'hours_down' })).toBe(
      'partly_observed',
    );
  });

  it('gives every verdict that needs a name a leaf that exists, and "No data" to none', () => {
    const verdicts = [
      'before_recording',
      'incidents_only',
      'partly_observed',
      'out_of_fleet',
    ] as const;
    for (const verdict of verdicts) {
      const leaf = CELL_ABSENCE_LABEL_KEY[verdict];
      expect(typeof lookup(leaf), `${verdict} names a leaf`).toBe('string');
      // The `unobserved` class is still "No data" where that IS the answer —
      // a share of a Time-split bar. It is never a CELL's reason, because
      // three of the four verdicts here sit beside a figure.
      expect(leaf, `${verdict} must not reuse the No-data leaf`).not.toBe(
        'fleetMetrics.class.unobserved',
      );
    }
  });

  it('marks exactly the two partly-observed verdicts as wearing the marker', () => {
    expect(isPartlyObserved('incidents_only')).toBe(true);
    expect(isPartlyObserved('partly_observed')).toBe(true);
    for (const quiet of ['zero', 'before_recording', 'out_of_fleet', null] as const) {
      expect(isPartlyObserved(quiet), `${String(quiet)} wears no marker`).toBe(false);
    }
  });

  it('keeps the texture off the digits on a cell that shows one', () => {
    // The full-face hatch strokes in `currentColor`, which on a heat tile is
    // the figure's own colour — so the stripes ran through the number the cell
    // exists to show. A cell WITH a figure gets a band along the bottom edge
    // instead; one with nothing to show keeps the whole face.
    const band = partialMarkerCss(true);
    const full = partialMarkerCss(false);

    expect(band.backgroundImage).toBe(full.backgroundImage);
    expect(band.backgroundSize).toBe(`100% ${FLEET_PARTIAL_BAND_PX}px`);
    expect(band.backgroundPosition).toBe('left bottom');
    expect(band.backgroundRepeat).toBe('no-repeat');
    // The full hatch is unbounded in both axes, which is what covered the text.
    expect(full.backgroundSize).toBe('auto');
    expect(full.backgroundPosition).toBeUndefined();
    // Thin enough to sit under a figure in a 24 px cell, thick enough to read
    // as a texture rather than as a rule.
    expect(FLEET_PARTIAL_BAND_PX).toBeGreaterThan(1);
    expect(FLEET_PARTIAL_BAND_PX).toBeLessThan(8);
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

describe('the Prints lens has ONE numerator', () => {
  const cell = (prints: Record<string, number>): MatrixCell => ({
    class_seconds: { printing: 3600 },
    down_seconds: 0,
    prints: prints as MatrixCell['prints'],
    basis: 'observed',
  });

  it('reads COMPLETED prints, never every outcome', () => {
    // The lens answers "what did this printer make", and a failed or cancelled
    // print made nothing. Summing all four put two numerators in one lens: the
    // cells and the row Total counted everything while the Avg column read the
    // payload's completed-only rate, so a row showed Total 1,079 against an Avg
    // of 6.0 — a rate nobody could derive from the row they were looking at.
    const mixed = cell({ completed: 9, failed: 2, cancelled: 1, other: 1 });
    expect(completedPrints(mixed)).toBe(9);
    expect(lensValue(mixed, 'prints')).toBe(9);
    // …and it is emphatically not the total of the outcome split.
    expect(lensValue(mixed, 'prints')).not.toBe(sumMap(mixed.prints));
  });

  it('answers zero for a bucket that completed nothing, whatever else it did', () => {
    const noneFinished = cell({ failed: 3, cancelled: 2 });
    expect(lensValue(noneFinished, 'prints')).toBe(0);
    // Which makes the cell read as a genuine zero — the four absence
    // treatments are unchanged, and the outcome split is in the detail.
    expect(cellAbsence(noneFinished, { elapsed_seconds: 3600, observed_seconds: 3600 }, { lens: 'prints' })).toBe('zero');
  });

  it('leaves the other two lenses on their own numerators', () => {
    const busy = { ...cell({ completed: 4, failed: 4 }), down_seconds: 600 };
    expect(lensValue(busy, 'hours_down')).toBe(600);
    expect(lensValue(busy, 'time_split')).toBe(3600);
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

describe('windowPrecedesRecording', () => {
  // Production, as of this wave: the recorder is an hour old and the window is
  // six weeks deep — so the tab has to say so on nearly every window an
  // operator picks, not only on a virgin instance.
  const TZ = 'Pacific/Auckland';

  it('is true when the window starts days before the recorder did', () => {
    expect(windowPrecedesRecording('2026-08-08', '2026-09-20T23:00:00', TZ)).toBe(true);
  });

  it('is false when the window starts after the recorder did', () => {
    expect(windowPrecedesRecording('2026-09-21', '2026-09-01T03:00:00', TZ)).toBe(false);
  });

  it('is true on the recorder’s own start DAY, whose first hours it missed', () => {
    // 2026-09-20T23:00:00 UTC is 11:00 on the 21st at the site, so a window
    // opening at the site's midnight predates it by eleven hours.
    expect(windowPrecedesRecording('2026-09-21', '2026-09-20T23:00:00', TZ)).toBe(true);
  });

  it('is true when nothing has ever been recorded', () => {
    expect(windowPrecedesRecording('2026-09-21', null, TZ)).toBe(true);
  });

  it('reads the SITE zone, not the browser’s', () => {
    // The same instant is the 20th in UTC and the 21st in Auckland, so a window
    // opening on the 21st precedes it only when the site zone is honoured.
    expect(windowPrecedesRecording('2026-09-21', '2026-09-20T23:00:00', 'UTC')).toBe(false);
    expect(windowPrecedesRecording('2026-09-21', '2026-09-20T23:00:00', TZ)).toBe(true);
  });

  it('makes no claim it cannot check', () => {
    expect(siteWallClock('not-an-instant', TZ)).toBeNull();
    expect(windowPrecedesRecording('2026-09-21', 'not-an-instant', TZ)).toBe(false);
  });

  it('falls back to UTC rather than throwing on a zone the browser lacks', () => {
    expect(siteWallClock('2026-09-20T23:00:00', 'Mars/Olympus')).toBe('2026-09-20T23:00:00');
  });
});

describe('a one-day window names its date once', () => {
  it('reads a same-day range as a single day', () => {
    expect(isSingleDayRange('2026-09-21', '2026-09-21')).toBe(true);
    expect(isSingleDayRange('2026-09-15', '2026-09-21')).toBe(false);
  });

  it('picks a different header leaf for each shape, loaded and pending', () => {
    const leaves = [
      headerRangeKey(true, false),
      headerRangeKey(false, false),
      headerRangeKey(true, true),
      headerRangeKey(false, true),
    ];
    expect(new Set(leaves).size).toBe(4);
    for (const leaf of leaves) expect(typeof lookup(leaf)).toBe('string');
  });

  it('picks a different matrix caption for each shape', () => {
    expect(matrixCaptionKey(true)).not.toBe(matrixCaptionKey(false));
    expect(typeof lookup(matrixCaptionKey(true))).toBe('string');
    expect(typeof lookup(matrixCaptionKey(false))).toBe('string');
  });

  it('captions the matrix BY printer, never "per printer"', () => {
    // "Prints per printer" is a metric on the same tab, and a Prints-lens
    // caption reading "Prints per printer, Sep 15 to Sep 21" named the rate
    // rather than the grid underneath it.
    for (const single of [true, false]) {
      expect(lookup(matrixCaptionKey(single))).not.toContain('per printer');
    }
  });
});

describe('overlapsBucket', () => {
  // The drill-down asks its endpoint for whole site DAYS, so an hour bucket's
  // answer routinely carries rows from the other twenty-three.
  const start = Date.parse('2026-09-19T16:00:00Z');
  const end = Date.parse('2026-09-19T17:00:00Z');
  const now = Date.parse('2026-09-21T00:04:00Z');
  const within = (span: { start: string; end: string | null }): boolean =>
    overlapsBucket(span, start, end, now);

  it('keeps a span that runs through the bucket', () => {
    expect(within({ start: '2026-09-19T16:10:00', end: '2026-09-19T16:40:00' })).toBe(true);
    expect(within({ start: '2026-09-19T15:00:00', end: '2026-09-19T18:00:00' })).toBe(true);
  });

  it('drops the incident that was listed under an hour it had nothing to do with', () => {
    // The production report: a 16:00–17:00 bucket listed an incident that ran
    // 19:18 the previous day to 00:37.
    expect(within({ start: '2026-09-18T19:18:00', end: '2026-09-19T00:37:00' })).toBe(false);
  });

  it('does not count touching as overlapping, at either edge', () => {
    expect(within({ start: '2026-09-19T15:00:00', end: '2026-09-19T16:00:00' })).toBe(false);
    expect(within({ start: '2026-09-19T17:00:00', end: '2026-09-19T18:00:00' })).toBe(false);
  });

  it('runs an open-ended span to the answer’s own now, and no further', () => {
    expect(within({ start: '2026-09-19T10:00:00', end: null })).toBe(true);
    // Opened after the bucket closed and still open: it overlaps later buckets,
    // not this one.
    expect(within({ start: '2026-09-20T10:00:00', end: null })).toBe(false);
  });

  it('keeps nothing it cannot place', () => {
    expect(within({ start: 'nonsense', end: '2026-09-19T16:30:00' })).toBe(false);
  });
});

describe('hour headers', () => {
  const at = (hourOfDay: number | null, hour: string | null) => ({ hourOfDay, hour });

  it('spells every third hour and nothing between', () => {
    // 14 px columns: labelling all twenty-four ran them into `000102030405…`.
    expect(showsHourLabel(at(0, '00:00'))).toBe(true);
    expect(showsHourLabel(at(3, '03:00'))).toBe(true);
    expect(showsHourLabel(at(21, '21:00'))).toBe(true);
    for (const hour of [1, 2, 4, 5, 7, 8, 22, 23]) {
      expect(showsHourLabel(at(hour, '')), `${hour} is unlabelled`).toBe(false);
    }
  });

  it('gives the labelled hours their two digits and the rest nothing', () => {
    expect(hourHeaderLabel(at(6, '06:00'), 'en')).toBe('06');
    expect(hourHeaderLabel(at(7, '07:00'), 'en')).toBe('');
  });

  it('labels no column on a lens that has no hour', () => {
    expect(showsHourLabel(at(null, null))).toBe(false);
    expect(hourHeaderLabel(at(null, null), 'en')).toBe('');
  });

  it('reads the hour NUMBER, never the first two characters of a clock', () => {
    // Slicing `label.hour` assumed the hour comes first AND that the cycle
    // runs 00–23. Neither is a locale-invariant fact; `hourOfDay` is.
    expect(hourHeaderLabel({ hourOfDay: 0 }, 'en')).toBe('00');
    expect(hourHeaderLabel({ hourOfDay: 21 }, 'en')).toBe('21');
  });
});

/**
 * Midnight is `00`, never `24`.
 *
 * `hour12: false` does NOT mean "the 00–23 cycle" — it means "this locale's own
 * 24-hour cycle", and ICU makes that **h24** (01–24) for several locales. A
 * bucket starting at the site's midnight therefore formatted as `24:00`: an
 * hour that does not exist, on the header row above the day it opens. Spelling
 * only every third hour is what made it visible, 00 being one of the three.
 */
describe('the site midnight hour', () => {
  /** `2026-09-20T12:00` naive UTC at +12 is `2026-09-21 00:00` at the site. */
  const MIDNIGHT: SeriesBucket<unknown> = {
    start: '2026-09-20T12:00:00',
    seconds: 3600,
    elapsed_seconds: 3600,
    observed_seconds: 3600,
    utc_offset_minutes: 720,
    basis: 'observed',
    values: {},
  };

  /** Every locale the app ships. */
  const LOCALES = ['en', 'de', 'es', 'fr', 'it', 'ja', 'ko', 'pt-BR', 'tr', 'zh-CN', 'zh-TW'];

  it('is 00 in every locale the app ships — the header and the full stamp', () => {
    for (const locale of LOCALES) {
      const label = bucketLabel(MIDNIGHT, { bucket: 'hour', locale });
      expect(label.hourOfDay, locale).toBe(0);
      expect(hourHeaderLabel(label, locale), locale).toBe('00');
      // `full` is the sr-only stamp on every hour column AND the bucket
      // detail's own title.
      expect(label.hour ?? '', locale).not.toMatch(/24/);
      expect(label.full, locale).not.toMatch(/24/);
    }
  });

  it('takes the h23 cycle BY NAME, not whatever the locale calls 24-hour', () => {
    // Measured against what `hour12: false` actually produces in THIS ICU
    // build rather than against a guessed locale: wherever that build spells
    // midnight with a 24, ours must disagree with it — and in every build,
    // ours must never contain one.
    for (const locale of LOCALES) {
      const localeOwn24Hour = new Intl.DateTimeFormat(locale, {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
        timeZone: 'UTC',
      }).format(Date.parse('2026-09-21T00:00:00Z'));
      const ours = bucketLabel(MIDNIGHT, { bucket: 'hour', locale }).hour ?? '';

      if (localeOwn24Hour.includes('24')) {
        expect(ours, `${locale} must not follow its own h24 cycle`).not.toBe(localeOwn24Hour);
      }
      expect(ours, locale).not.toMatch(/24/);
    }
  });

  it('leaves no hour12 option on any Fleet surface that spells a clock', () => {
    // The rule, not the instance. `utils/date.ts` owns the operator's real
    // 12/24-hour PREFERENCE and is untouched by this; a Fleet surface has no
    // such choice to make — it states a 24-hour clock — so an `hour12` here is
    // the h24 bug waiting to come back.
    const owner = path.join(SRC, 'utils', 'fleetMetrics.ts');
    // CODE, not prose: the comments above and in the modules themselves name
    // the flag in order to explain why it is not used.
    const stripComments = (source: string): string =>
      source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');

    const offenders: string[] = [];
    const inspect = (entry: string): void => {
      if (fs.statSync(entry).isDirectory()) {
        for (const child of fs.readdirSync(entry)) inspect(path.join(entry, child));
        return;
      }
      if (!/\.tsx?$/.test(entry)) return;
      if (/hour12\s*:/.test(stripComments(fs.readFileSync(entry, 'utf8')))) {
        offenders.push(path.relative(SRC, entry));
      }
    };
    inspect(owner);
    inspect(path.join(SRC, 'components', 'fleet'));

    expect(offenders).toEqual([]);
    expect(fs.readFileSync(owner, 'utf8')).toMatch(/hourCycle: 'h23'/);
  });
});

describe('a bucket that has not happened', () => {
  const at = (elapsed: number, observed: number) => ({
    elapsed_seconds: elapsed,
    observed_seconds: observed,
  });

  it('is told from one that happened and recorded nothing', () => {
    expect(bucketHasElapsed(at(0, 0))).toBe(false);
    expect(bucketHasElapsed(at(3600, 0))).toBe(true);
  });

  it('takes the upcoming verdict BEFORE any question about data', () => {
    // On Today at 17:00 seven of the twenty-four hours have not started. They
    // read as ledger-only zeros — a full hatch and an sr-only "0 Faults and
    // holds only" — over a drill-down that correctly said there was no data.
    const future = cell({ class_seconds: {}, basis: 'incidents_only' });
    for (const lens of ['prints', 'hours_down', 'time_split'] as const) {
      expect(cellAbsence(future, at(0, 0), { lens }), lens).toBe('upcoming');
    }
  });

  it('is never mistaken for a zero, a recorder gap or a deactivation', () => {
    const future = cell({ class_seconds: {}, basis: 'incidents_only' });
    const verdict = cellAbsence(future, at(0, 0), { lens: 'hours_down' });
    expect(verdict).not.toBe('zero');
    expect(verdict).not.toBe('incidents_only');
    expect(verdict).not.toBe('out_of_fleet');
    // …and it wears no partly-observed marker: there was nothing to miss.
    expect(isPartlyObserved(verdict)).toBe(false);
  });

  it('names itself with a leaf that exists', () => {
    expect(CELL_ABSENCE_LABEL_KEY.upcoming).toBe('fleetMetrics.class.upcoming');
    expect(typeof lookup(CELL_ABSENCE_LABEL_KEY.upcoming)).toBe('string');
  });
});

describe('currentBucketIndex', () => {
  const grid = (...elapsed: number[]) => elapsed.map((value) => ({ elapsed_seconds: value }));

  it('is the last bucket that has ELAPSED, not the last bucket', () => {
    // Today's hour grid: the per-row Details control opened
    // `buckets.length - 1`, which is 23:00 — so a printer with seventeen hours
    // down today opened an empty dialog for an hour that has not arrived.
    expect(currentBucketIndex(grid(3600, 3600, 1800, 0, 0))).toBe(2);
  });

  it('is the last bucket for a window wholly in the past', () => {
    expect(currentBucketIndex(grid(3600, 3600, 3600))).toBe(2);
  });

  it('answers a usable index for a grid with nothing elapsed at all', () => {
    expect(currentBucketIndex(grid(0, 0, 0))).toBe(2);
    expect(currentBucketIndex([])).toBe(0);
  });
});

describe('isolatedPointFlags', () => {
  it('marks the point that has no neighbour to join', () => {
    // "Prints per printer" exists for observed buckets alone, so in the first
    // days there is exactly one — and a one-point line is a zero-length path
    // that paints nothing while the legend promises a line.
    expect(isolatedPointFlags([null, null, 4.2, null])).toEqual([false, false, true, false]);
  });

  it('leaves a run of two or more as a plain line', () => {
    expect(isolatedPointFlags([null, 1, 2, null])).toEqual([false, false, false, false]);
    expect(isolatedPointFlags([1, 2, 3])).toEqual([false, false, false]);
  });

  it('counts the ends as absent, so a lone first or last point is isolated', () => {
    expect(isolatedPointFlags([7, null, null])).toEqual([true, false, false]);
    expect(isolatedPointFlags([null, null, 7])).toEqual([false, false, true]);
    expect(isolatedPointFlags([7])).toEqual([true]);
  });

  it('marks each island of one separately', () => {
    expect(isolatedPointFlags([1, null, 2, null, 3])).toEqual([true, false, true, false, true]);
  });

  it('never marks an absent point', () => {
    expect(isolatedPointFlags([null, undefined, null])).toEqual([false, false, false]);
  });
});

describe('the Fleet row’s Avg', () => {
  it('sums under Prints and means under the two hours lenses', () => {
    expect(LENS_FLEET_AVG_MODE.prints).toBe('sum');
    expect(LENS_FLEET_AVG_MODE.hours_down).toBe('mean');
    // Time split's Avg column carries hours down per day too.
    expect(LENS_FLEET_AVG_MODE.time_split).toBe('mean');
  });

  it('never exceeds a day when every printer is down all day', () => {
    // The defect: twelve printers at roughly 20 h down per day summed to a
    // Fleet "Avg" of 86 h/day, beside rows reading 22 and 18.
    const allDown = Array.from({ length: 12 }, () => 24);
    expect(fleetAverage(allDown, 'mean')).toBe(24);
    expect(fleetAverage(allDown, 'sum')).toBe(288);
  });

  it('is directly comparable with the rows above it', () => {
    expect(fleetAverage([22, 21, 20, 18], 'mean')).toBeCloseTo(20.25, 6);
  });

  it('leaves a printer with no denominator out of the mean, never in as a zero', () => {
    // A printer the recorder never counted prints "–" in its own Avg cell, and
    // averaging it in as a zero is the unknown-read-as-zero conflation the rest
    // of this module exists to prevent.
    expect(fleetAverage([24, 24, null, null], 'mean')).toBe(24);
    expect(fleetAverage([24, 24, null, null], 'sum')).toBe(48);
  });

  it('is null when no printer has a rate — a mean of nothing is not zero', () => {
    expect(fleetAverage([null, null], 'mean')).toBeNull();
    expect(fleetAverage([], 'sum')).toBeNull();
  });

  it('describes each mode with a leaf that exists, and the two differ', () => {
    const mean = FLEET_AVG_DESCRIPTION_KEY.mean;
    const sum = FLEET_AVG_DESCRIPTION_KEY.sum;
    expect(mean).not.toBe(sum);
    for (const leaf of [mean, sum]) expect(typeof lookup(leaf)).toBe('string');
  });
});

describe('the Today chip', () => {
  const bucketAt = (seconds: number, elapsed: number): SeriesBucket<unknown> => ({
    start: '2026-09-20T12:00:00',
    seconds,
    elapsed_seconds: elapsed,
    observed_seconds: elapsed,
    utc_offset_minutes: 720,
    basis: 'observed',
    values: {},
  });

  it('rides the current DAY bucket, and only that', () => {
    const day = bucketLabel(bucketAt(86_400, 43_200), { bucket: 'day', locale: 'en' });
    expect(day.isCurrent).toBe(true);
    expect(day.showsTodayChip).toBe(true);
  });

  it('never rides an hour column', () => {
    // It sat on ONE hour column of an all-today hour grid — calling one hour of
    // today "Today" while the twenty-three beside it are the same day, and
    // breaking the every-third-hour cadence where it sat.
    const hour = bucketLabel(bucketAt(3600, 1800), { bucket: 'hour', locale: 'en' });
    expect(hour.isCurrent).toBe(true);
    expect(hour.showsTodayChip).toBe(false);
  });

  it('never rides a week column — the current week is named by its own start', () => {
    const week = bucketLabel(bucketAt(604_800, 302_400), { bucket: 'week', locale: 'en' });
    expect(week.isCurrent).toBe(true);
    expect(week.showsTodayChip).toBe(false);
    expect(week.weekStart).not.toBeNull();
  });

  it('rides no FINISHED bucket, at any width', () => {
    for (const width of ['hour', 'day', 'week'] as const) {
      const seconds = BUCKET_HOURS[width] * 3600;
      const finished = bucketLabel(bucketAt(seconds, seconds), { bucket: width, locale: 'en' });
      expect(finished.showsTodayChip, width).toBe(false);
    }
  });
});

describe('formatTickCount', () => {
  it('groups a tick a reader can still parse', () => {
    expect(formatTickCount(900, 'en')).toBe('900');
    expect(formatTickCount(9999, 'en')).toBe('9,999');
  });

  it('goes compact where the sixth glyph would arrive', () => {
    // A five-digit tick is what clipped the axis; three glyphs fit the column
    // the axis was already sized for.
    expect(formatTickCount(10_000, 'en')).toBe('10K');
    expect(formatTickCount(12_500, 'en')).toBe('12.5K');
  });

  it('follows the active locale', () => {
    expect(formatTickCount(1234, 'de')).toBe('1.234');
  });
});

describe('summary row units and hints', () => {
  it('units exactly the two rows that are a RATE, and nothing else', () => {
    // `SUMMARY_ROW_FORMAT` says how a number is spelled, never what it counts
    // per: "Prints 21" beside a matrix total of 900 was a rate read as a count.
    expect(Object.keys(SUMMARY_ROW_UNIT_KEY).sort()).toEqual([
      'prints_per_day',
      'prints_per_printer_per_day',
    ]);
    for (const leaf of Object.values(SUMMARY_ROW_UNIT_KEY)) {
      expect(typeof lookup(leaf as string)).toBe('string');
    }
  });

  it('qualifies a hinted row when the window outruns the recorder, and only then', () => {
    const settled = summaryHintKeys('uptime', { recordingGap: false });
    const gapped = summaryHintKeys('uptime', { recordingGap: true });
    expect(gapped.slice(0, settled.length)).toEqual(settled);
    expect(gapped.length).toBe(settled.length + 1);
    for (const leaf of gapped) expect(typeof lookup(leaf)).toBe('string');
  });

  it('gives no row a tooltip it would not otherwise have had', () => {
    // Supplementary copy rides a control that already carries some; a row whose
    // label says it all does not grow a trigger just to hold a qualifier.
    expect(summaryHintKeys('avg_printing', { recordingGap: true })).toEqual([]);
    expect(summaryHintKeys('peak_down', { recordingGap: true })).toEqual([]);
  });

  it('says the two prints rates are not comparable while the window outruns the recorder', () => {
    // Counted printer-days come from RECORDED buckets only, while prints per
    // day covers the whole window — during the first days they are two
    // different denominators and the row has to say so.
    const hint = lookup('fleetMetrics.hints.prints_per_printer');
    expect(typeof hint).toBe('string');
    expect(hint as string).toMatch(/recorded/i);
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
