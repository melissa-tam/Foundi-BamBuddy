/**
 * Liveness pin for the i18n plural shape.
 *
 * `scripts/check-i18n-parity.mjs` check 3 owns SHAPE — which suffixes may exist
 * on disk. This file owns RESOLUTION: that the surviving shape actually renders
 * a plural through the app's OWN i18next instance (its init options, i18next 25,
 * and the platform's `Intl.PluralRules`). The two are not the same fact — a
 * `_plural` key passes every static key-set check and still renders the singular
 * at every count, which is exactly how the dead convention survived. See the
 * statement above `.init({` in `src/i18n/index.ts`.
 *
 * Families are every base carrying ANY of `_one` / `_other` / `_plural` in ANY
 * locale, so a reintroduced `_plural` family is walked and FAILS here rather
 * than being silently skipped.
 *
 * Locales are registered explicitly below: under test the app instance boots on
 * the `en` baseline alone, and this is the one file that resolves through all
 * eleven.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
// @ts-expect-error -- .mjs script with no type declarations; pure JS import is fine for tests
import { loadLocale } from '../../../scripts/check-i18n-parity.mjs';
import i18n from '../../i18n';
import { allResources } from '../../i18n/resources';

// `src/i18n/index.ts` boots the instance with `en` only under test, so that the
// other 228 test files do not each evaluate 4.0 MB of locale modules they never
// read. This pin needs the real thing: register the shipped set on the app's own
// instance. `addResourceBundle` deep-copies what it is handed — that cost is
// paid once, here, in the only file that asks for it.
for (const [lng, bundle] of Object.entries(allResources)) {
  i18n.addResourceBundle(lng, 'translation', bundle.translation);
}

const readLocale = loadLocale as (filePath: string) => Map<string, string>;
const translate = (key: string, options: Record<string, unknown>): string => i18n.t(key, options);

const localesDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../i18n/locales');

const SUFFIXES = ['_one', '_other', '_plural'] as const;
const COUNTS = [0, 1, 2] as const;

const localeCodes: string[] = fs
  .readdirSync(localesDir)
  .filter((f) => f.endsWith('.ts'))
  .map((f) => f.slice(0, -3))
  .sort();

const localeMaps = new Map<string, Map<string, string>>(
  localeCodes.map((code) => [code, readLocale(path.join(localesDir, `${code}.ts`))]),
);

const leavesFor = (code: string): Map<string, string> => {
  const map = localeMaps.get(code);
  if (!map) throw new Error(`no leaf map loaded for locale "${code}"`);
  return map;
};

/** Every base that any locale plural-gates, however wrongly. */
const families: string[] = [
  ...new Set(
    [...localeMaps.values()].flatMap((map) =>
      [...map.keys()].flatMap((key) => {
        const suffix = SUFFIXES.find((s) => key.endsWith(s));
        return suffix ? [key.slice(0, -suffix.length)] : [];
      }),
    ),
  ),
].sort();

/**
 * Every non-`count` placeholder the family uses in any locale, supplied as its
 * own name so the pin never leans on i18next's `skipOnVariables` default.
 */
const varsFor = (base: string): Record<string, string> => {
  const names = new Set<string>();
  for (const map of localeMaps.values()) {
    for (const key of [base, ...SUFFIXES.map((s) => `${base}${s}`)]) {
      const value = map.get(key);
      if (value === undefined) continue;
      for (const match of value.matchAll(/\{\{([^{}]+)\}\}/g)) names.add(match[1].trim());
    }
  }
  names.delete('count');
  return Object.fromEntries([...names].map((name) => [name, name]));
};

const interpolate = (template: string, values: Record<string, string | number>): string =>
  template.replace(/\{\{([^{}]+)\}\}/g, (_match, raw: string) => String(values[raw.trim()]));

describe('i18n plural families', () => {
  it('discovers the bundled locales and at least one plural family', () => {
    expect(localeCodes).toContain('en');
    expect(families.length).toBeGreaterThan(0);
  });

  /**
   * The registration above is what makes every `lng` below resolvable. If a
   * future change drops it, or `allResources` loses a locale, the per-locale
   * assertions fall back to `en` and fail — but they fail late and confusingly,
   * so state the precondition here.
   */
  it('has every discovered locale registered on the app instance', () => {
    for (const code of localeCodes) {
      expect(Object.keys(i18n.store.data), `${code} is not registered`).toContain(code);
    }
  });

  for (const base of families) {
    const vars = varsFor(base);

    describe(base, () => {
      for (const lng of localeCodes) {
        it(`resolves a plural in ${lng}`, () => {
          const leaves = leavesFor(lng);

          // Shape: exactly one sanctioned pair, no dead or masking sibling.
          expect(leaves.has(`${base}_one`), `${lng}: ${base}_one is missing`).toBe(true);
          expect(leaves.has(`${base}_other`), `${lng}: ${base}_other is missing`).toBe(true);
          expect(
            leaves.has(`${base}_plural`),
            `${lng}: ${base}_plural never resolves under i18next 25 — rename it to ${base}_other`,
          ).toBe(false);
          expect(
            leaves.has(base),
            `${lng}: bare ${base} masks a missing plural form as the singular`,
          ).toBe(false);

          // Resolution: the instance selects the CLDR form this locale's rules name.
          for (const count of COUNTS) {
            const category = new Intl.PluralRules(lng).select(count);
            const template = leaves.get(`${base}_${category}`);
            if (template === undefined) {
              throw new Error(`${lng}: ${base}_${category} is missing — required at count=${count}`);
            }
            expect(
              translate(base, { count, lng, ...vars }),
              `${lng}: ${base} at count=${count} (CLDR "${category}")`,
            ).toBe(interpolate(template, { count, ...vars }));
          }
        });
      }
    });
  }
});
