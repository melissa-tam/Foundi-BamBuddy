/**
 * Chart chrome — the one origin, and the source pins that keep it the only one.
 *
 * The pins exist because this module was created by collecting five hand-typed
 * copies of the same tooltip panel, grid and axis styling that had drifted into
 * three backgrounds, two border radii and three greys. A copy is cheap to type
 * and invisible in review, so it is a test that has to notice.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import {
  CHART_AXIS_STROKE,
  CHART_GRID_DASH,
  CHART_GRID_STROKE,
  CHART_LEGEND_TEXT_STYLE,
  CHART_MUTED_TEXT,
  CHART_TICK_FONT_SIZE,
  CHART_TOOLTIP_CONTENT_STYLE,
  CHART_TOOLTIP_ITEM_STYLE,
  CHART_TOOLTIP_LABEL_STYLE,
  chartAxisTick,
} from '../../utils/chartChrome';

const SRC_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

function tsxFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
      found.push(...tsxFiles(full));
    } else if (entry.name.endsWith('.tsx')) {
      found.push(full);
    }
  }
  return found;
}

const relative = (file: string): string => path.relative(SRC_DIR, file).replace(/\\/g, '/');

describe('chart chrome values', () => {
  const styleObjects = {
    CHART_TOOLTIP_CONTENT_STYLE,
    CHART_TOOLTIP_LABEL_STYLE,
    CHART_TOOLTIP_ITEM_STYLE,
    CHART_LEGEND_TEXT_STYLE,
  };

  it('exports frozen style objects, so no consumer can mutate the origin', () => {
    for (const [name, style] of Object.entries(styleObjects)) {
      expect(`${name} frozen: ${Object.isFrozen(style)}`).toBe(`${name} frozen: true`);
    }
    expect(Object.isFrozen(chartAxisTick())).toBe(true);
  });

  it('carries no bare hex — every colour is a theme variable', () => {
    const colourKeys = ['color', 'backgroundColor', 'border', 'fill', 'stroke'];
    const colours: string[] = [CHART_AXIS_STROKE, CHART_GRID_STROKE, CHART_MUTED_TEXT];
    for (const style of Object.values(styleObjects)) {
      for (const [key, value] of Object.entries(style)) {
        if (colourKeys.includes(key) && typeof value === 'string') colours.push(value);
      }
    }
    colours.push(String(chartAxisTick().fill));

    expect(colours.filter((value) => value.includes('#'))).toEqual([]);
    expect(colours.filter((value) => !value.includes('var(--'))).toEqual([]);
    // Every style object actually carries a colour — the check above cannot pass vacuously.
    expect(colours.length).toBeGreaterThanOrEqual(3 + Object.keys(styleObjects).length);
  });

  it('never derives from the user-selectable accent', () => {
    const everything = JSON.stringify({ ...styleObjects, CHART_AXIS_STROKE, CHART_GRID_STROKE });
    expect(everything).not.toContain('--accent');
  });

  it('gives ticks the axis colour and the caller-chosen size', () => {
    expect(chartAxisTick()).toEqual({ fill: CHART_AXIS_STROKE, fontSize: CHART_TICK_FONT_SIZE });
    expect(chartAxisTick(9)).toEqual({ fill: CHART_AXIS_STROKE, fontSize: 9 });
  });

  it('keeps the grid dash the charts already used', () => {
    expect(CHART_GRID_DASH).toBe('3 3');
  });
});

describe('chart chrome source pins', () => {
  const files = tsxFiles(SRC_DIR);

  it('finds the chart components to scan', () => {
    expect(files.length).toBeGreaterThan(50);
    expect(files.map(relative)).toContain('pages/StatsPage.tsx');
    expect(files.map(relative)).toContain('components/FilamentTrends.tsx');
  });

  it('leaves no recharts tooltip panel defined outside chartChrome.ts', () => {
    const offenders: string[] = [];
    for (const file of files) {
      const source = fs.readFileSync(file, 'utf8');
      for (const match of source.matchAll(/contentStyle=\{\{([\s\S]*?)\}\}/g)) {
        if (/\bbackground(Color)?\s*:/.test(match[1])) {
          offenders.push(`${relative(file)}: contentStyle={{${match[1].trim()}}}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('leaves no literal grid colour on a CartesianGrid', () => {
    const offenders: string[] = [];
    for (const file of files) {
      const source = fs.readFileSync(file, 'utf8');
      for (const match of source.matchAll(/<CartesianGrid[^>]*>/g)) {
        if (/stroke="#/.test(match[0])) offenders.push(`${relative(file)}: ${match[0]}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('leaves no literal axis colour on an XAxis or YAxis', () => {
    const offenders: string[] = [];
    for (const file of files) {
      const source = fs.readFileSync(file, 'utf8');
      for (const match of source.matchAll(/<[XY]Axis[\s\S]{0,400}?\/>/g)) {
        if (/stroke="#|fill: '#|fill: "#/.test(match[0])) {
          offenders.push(`${relative(file)}: ${match[0].split('\n')[0]}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
