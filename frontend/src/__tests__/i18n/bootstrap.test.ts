/**
 * Pin for the i18n bootstrap split.
 *
 * `src/i18n/index.ts` carries the `en` baseline only; the other ten locales
 * reach the instance through `src/i18n/resources.ts`, which PRODUCTION loads
 * via `src/i18n/boot.ts` before `src/App.tsx` is evaluated.
 *
 * Both halves of that split are invisible to the rest of the suite, which is
 * exactly why they need a gate:
 *
 *   - Nothing else exercises the production path. Drop the `./i18n/boot` import
 *     from `src/main.tsx` and the app ships English-only while all 229 test
 *     files stay green.
 *   - Nothing else notices the saving going away. Re-add a locale import to
 *     `src/i18n/index.ts` and every test file silently pays 4.0 MB of module
 *     evaluation again, with no failure anywhere.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import i18n, { availableLanguages } from '../../i18n';

// `../../i18n/resources` is read as TEXT, never imported: importing it would
// make this file the second one to evaluate all 4.1 MB of locale modules, which
// is the cost this pin exists to protect. `plurals.test.ts` is the only file
// that genuinely needs them loaded.
const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = (relativePath: string): string =>
  fs.readFileSync(path.join(srcDir, relativePath), 'utf8');

describe('i18n bootstrap', () => {
  it('registers only the en baseline under test', () => {
    // The whole point of the split. `plurals.test.ts` opts back in explicitly.
    expect(Object.keys(i18n.store.data)).toEqual(['en']);
  });

  it('leaves the instance ready to translate on import', () => {
    // Nine test files and src/__tests__/setup.ts import this module and expect
    // a live instance, so the MODE branch in src/i18n/index.ts must have fired.
    expect(i18n.isInitialized).toBe(true);
    // A real lookup, not the key echoed back: src/i18n/locales/en.ts:501.
    expect(i18n.t('common.save')).toBe('Save');
  });

  it('keeps every locale module out of the instance module', () => {
    const source = read('i18n/index.ts');
    const localeImports = [...source.matchAll(/from '\.\/locales\/([\w-]+)'/g)].map((m) => m[1]);
    expect(localeImports).toEqual(['en']);
    expect(source).not.toMatch(/from '\.\/resources'/);
  });

  it('boots production with every shipped locale, before the app graph', () => {
    const main = read('main.tsx');
    expect(main).toMatch(/import '\.\/i18n\/boot'/);
    // Ordering is load-bearing: boot must precede ./App so init runs before any
    // component module is evaluated (see src/i18n/boot.ts).
    expect(main.indexOf("'./i18n/boot'")).toBeLessThan(main.indexOf("./App"));
    expect(read('i18n/boot.ts')).toMatch(/initI18n\(allResources\)/);
  });

  it('ships the same eleven locales the language picker offers', () => {
    const declared = [...read('i18n/resources.ts').matchAll(/^ {2}'?([\w-]+)'?: \{ translation:/gm)]
      .map((m) => m[1]);
    expect(declared).toEqual(availableLanguages.map((l) => l.code));
    expect(availableLanguages).toHaveLength(11);
  });
});
