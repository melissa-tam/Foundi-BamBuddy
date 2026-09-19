/**
 * Structural pin: `hooks/useProductionRunDetail` is THE `['production-runs', id]`
 * query.
 *
 * Two surfaces read a run's detail with deliberately different polling
 * appetites (the detail page polls, a list card does not). A second `useQuery`
 * on that key would give one of them the other's `refetchInterval` — which is
 * exactly the regression this pin exists for, since the backend's
 * `_build_printer_eligibility` runs a per-printer deficit computation on every
 * fetch. A new caller passes `{ poll }` instead of declaring its own query.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

/** The hook module itself, as a src-relative posix path. */
const HOOK = 'hooks/useProductionRunDetail.ts';

/** Every .ts/.tsx under src/pages, src/components and src/hooks. */
function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(path.join(srcDir, dir), { withFileTypes: true })) {
      const rel = `${dir}/${entry.name}`;
      if (entry.isDirectory()) walk(rel);
      else if (/\.tsx?$/.test(entry.name)) out.push(rel);
    }
  };
  for (const root of ['pages', 'components', 'hooks']) walk(root);
  return out;
}

const read = (relativePath: string): string =>
  fs.readFileSync(path.join(srcDir, relativePath), 'utf8');

describe('useProductionRunDetail is the one production-run detail query', () => {
  it('finds the source files it is meant to scan', () => {
    const files = sourceFiles();
    expect(files).toContain(HOOK);
    expect(files).toContain('pages/ProductionRunsPage.tsx');
    expect(files).toContain('pages/ProductionRunDetailPage.tsx');
  });

  it('is the only module that declares a keyed ["production-runs", id] query', () => {
    // An IDENTIFIER second segment (a run id) only — the string-segment key
    // ['production-runs', 'printer-states'] is a different endpoint and stays
    // where it is.
    const keyedDetailQuery = /queryKey:\s*\[\s*'production-runs'\s*,\s*[A-Za-z_$]/;
    const offenders = sourceFiles().filter((file) => keyedDetailQuery.test(read(file)));

    expect(offenders).toEqual([HOOK]);
  });

  it('is the only module that calls api.getProductionRun', () => {
    const offenders = sourceFiles().filter((file) => read(file).includes('api.getProductionRun('));

    expect(offenders).toEqual([HOOK]);
  });
});
