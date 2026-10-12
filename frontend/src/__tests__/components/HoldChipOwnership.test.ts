/**
 * Structural pin: the hold chip's VERDICT is the backend's (`services/hold_chip`,
 * 2026-10-11). The card shows `PrinterStatus.hold_chip` as it is sent and decides
 * nothing — no precedence (which hold), no tone, no reason → copy table.
 *
 * Before it the frontend ranked the open row against the toolhead itself and keyed
 * "Toolhead empty" off the ROW's label, so a hold a stale build escalated showed red
 * "Toolhead empty" all night over a toolhead the wire reported fed (001-H2S incident
 * 574). This fails when such a table comes back into the frontend source.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

/** Every .ts/.tsx under src, tests excluded. */
function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(path.join(srcDir, dir), { withFileTypes: true })) {
      const rel = dir ? `${dir}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        if (entry.name !== '__tests__') walk(rel);
      } else if (/\.tsx?$/.test(entry.name)) out.push(rel);
    }
  };
  walk('');
  return out;
}

const read = (relativePath: string): string => fs.readFileSync(path.join(srcDir, relativePath), 'utf8');

/** The tell-tales of a chip decided in the browser: its tables, its keyed lookups, its module. */
const CHIP_TABLES: ReadonlyArray<[string, RegExp]> = [
  ['the recovering-tooltip kinds', /RECOVERING_ACTION_KINDS/],
  ['the paused-empty verdict copy', /PAUSED_EMPTY_TOOLTIP/],
  ['the failed-refill copy', /FAILED_COPY/],
  ['a recovering tooltip keyed off a kind', /incidentRecoveringAction\.\$\{/],
  ['an action tooltip keyed off a kind', /incidentAction\.\$\{/],
  ['a precedence function over the status', /function\s+holdChip\s*\(/],
];

describe('the hold chip is rendered, never decided, in the browser', () => {
  it('finds the source files it is meant to scan', () => {
    const files = sourceFiles();
    expect(files).toContain('components/HoldChip.tsx');
    expect(files).toContain('pages/PrintersPage.tsx');
  });

  it('has no utils/incidentChip module', () => {
    expect(sourceFiles()).not.toContain('utils/incidentChip.ts');
  });

  it.each(CHIP_TABLES)('declares no %s', (_label, pattern) => {
    const offenders = sourceFiles().filter((file) => pattern.test(read(file)));
    expect(offenders).toEqual([]);
  });

  it('reads the chip off the status payload', () => {
    expect(read('pages/PrintersPage.tsx')).toMatch(/status\?\.hold_chip/);
  });
});
