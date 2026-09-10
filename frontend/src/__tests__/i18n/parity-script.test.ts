import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
// @ts-expect-error -- .mjs script with no type declarations; pure JS import is fine for tests
import { compareLocales, loadLocale } from '../../../scripts/check-i18n-parity.mjs';

type LocaleMap = Map<string, string>;

const readLocale = loadLocale as (filePath: string) => LocaleMap;

const toMap = (obj: Record<string, string>): LocaleMap => new Map(Object.entries(obj));

const hasReport = (
  reports: Array<{ label: string; items: string[] }>,
  labelSubstr: string,
  itemSubstr?: string,
): boolean =>
  reports.some(
    (r) =>
      r.label.includes(labelSubstr) &&
      (itemSubstr === undefined || r.items.some((i) => i.includes(itemSubstr))),
  );

describe('compareLocales (parity-script self-test)', () => {
  it('passes when all locales match en', () => {
    const en = toMap({ 'a.b': 'hello {{name}}', 'count_one': 'one', 'count_other': 'many' });
    const result = compareLocales({
      en,
      'zh-CN': toMap({ 'a.b': '你好 {{name}}', 'count_one': '一', 'count_other': '多' }),
      'zh-TW': toMap({ 'a.b': '你好 {{name}}', 'count_one': '一', 'count_other': '多' }),
    });
    expect(result.failed).toBe(false);
    expect(result.reports).toEqual([]);
  });

  it('flags keys missing from a non-en locale', () => {
    const result = compareLocales({
      en: toMap({ 'a.b': 'x', 'a.c': 'y' }),
      'zh-TW': toMap({ 'a.b': 'x' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'zh-TW: missing keys vs en', 'a.c')).toBe(true);
  });

  it('flags keys that exist in a non-en locale but not in en', () => {
    const result = compareLocales({
      en: toMap({ 'a.b': 'x' }),
      'zh-CN': toMap({ 'a.b': 'x', 'a.stray': 'extra' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'zh-CN: extra keys vs en', 'a.stray')).toBe(true);
  });

  it('flags placeholder mismatch (missing placeholder in translation)', () => {
    const result = compareLocales({
      en: toMap({ greeting: 'Hello {{name}}!' }),
      'zh-CN': toMap({ greeting: '你好!' }), // {{name}} dropped
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'placeholder mismatch', 'greeting')).toBe(true);
  });

  it('flags placeholder mismatch (translation introduces unknown placeholder)', () => {
    // This is the exact class of bug the zh-CN sync caught:
    // fileManager.uploadFailed had a stray {{count}} copied from a sibling key.
    const result = compareLocales({
      en: toMap({ uploadFailed: 'Upload failed' }),
      'zh-CN': toMap({ uploadFailed: '{{count}} 个失败' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'placeholder mismatch', 'uploadFailed')).toBe(true);
  });

  // --- check 3: plural SHAPE, evaluated on en only -------------------------
  // i18next 25 (compatibilityJSON unset) resolves `_one` / `_other` and nothing
  // else — see the statement above `.init({` in src/i18n/index.ts. Check 1
  // mirrors en's key set into every locale, so a shape stated once on en is a
  // shape enforced everywhere; the non-en direction belongs to check 1.

  it('flags a dead _plural suffix in en', () => {
    const result = compareLocales({
      en: toMap({ item_one: 'item', item_plural: 'items' }),
      'zh-CN': toMap({ item_one: '项', item_plural: '项' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'en: plural shape', 'dead plural suffix')).toBe(true);
  });

  it('leaves a _plural present only in a non-en locale to check 1 (extra key)', () => {
    const result = compareLocales({
      en: toMap({ item_one: 'item', item_other: 'items' }),
      'zh-CN': toMap({ item_one: '项', item_other: '项', item_plural: '项' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'zh-CN: extra keys vs en', 'item_plural')).toBe(true);
  });

  it('flags _one without a matching _other', () => {
    const result = compareLocales({
      en: toMap({ item_one: 'item' }),
      'zh-CN': toMap({ item_one: '项' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'en: plural shape', '_one without _other')).toBe(true);
  });

  it('flags _other without a matching _one (catches a typo\'d half, e.g. item_ohter)', () => {
    const result = compareLocales({
      en: toMap({ item_other: 'items' }),
      'zh-CN': toMap({ item_other: '项' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'en: plural shape', '_other without _one')).toBe(true);
  });

  it('flags a bare key sharing a plural pair\'s base', () => {
    const result = compareLocales({
      en: toMap({ item: 'item', item_one: 'item', item_other: 'items' }),
      'zh-CN': toMap({ item: '项', item_one: '项', item_other: '项' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'en: plural shape', 'bare sibling of plural pair')).toBe(true);
  });

  it('flags an unsupported plural form (_zero) even though i18next would resolve it', () => {
    const result = compareLocales({
      en: toMap({ item_zero: 'none', item_one: 'item', item_other: 'items' }),
      'zh-CN': toMap({ item_zero: '无', item_one: '项', item_other: '项' }),
    });
    expect(result.failed).toBe(true);
    expect(hasReport(result.reports, 'en: plural shape', 'unsupported plural form')).toBe(true);
  });

  it('passes the sanctioned nested pair shape', () => {
    const result = compareLocales({
      en: toMap({
        'queue.itemCount_one': '{{count}} item',
        'queue.itemCount_other': '{{count}} items',
      }),
      'zh-CN': toMap({
        'queue.itemCount_one': '{{count}} 个项目',
        'queue.itemCount_other': '{{count}} 个项目',
      }),
    });
    expect(result.failed).toBe(false);
    expect(result.reports).toEqual([]);
  });

  it('reports nothing for the real locale files on disk', () => {
    const localesDir = path.resolve(
      path.dirname(fileURLToPath(import.meta.url)),
      '../../i18n/locales',
    );
    const codes = fs
      .readdirSync(localesDir)
      .filter((f) => f.endsWith('.ts'))
      .map((f) => f.slice(0, -3));
    expect(codes).toContain('en');
    const locales = Object.fromEntries(
      codes.map((code) => [code, readLocale(path.join(localesDir, `${code}.ts`))]),
    );

    const result = compareLocales(locales);
    expect(result.reports).toEqual([]);
    expect(result.failed).toBe(false);
  });

  it('throws when the en locale is absent', () => {
    expect(() =>
      compareLocales({ 'zh-CN': toMap({ a: 'x' }) } as Record<string, LocaleMap>),
    ).toThrow(/locales\.en/);
  });
});
