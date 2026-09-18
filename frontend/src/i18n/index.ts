import i18n from 'i18next';
import type { Resource } from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';

// `en` is the fallback locale and the ONLY bundle this module carries — the
// baseline the instance cannot answer a `t()` without. The other ten locales
// live in `./resources`, which production loads through `./boot`. See the
// statement on `initI18n` below for why the split exists and why it is safe.
import en from './locales/en';

/** The fallback-only resource set every environment starts from. */
const baseResources: Resource = { en: { translation: en } };

const SUPPORTED_LNGS = ['en', 'de', 'es', 'fr', 'ja', 'it', 'ko', 'pt-BR', 'tr', 'zh-CN', 'zh-TW'];
const APPLIANCE_CONSUMED_KEY = 'bambuddy_appliance_locale_consumed';

/**
 * Initialise THE app i18next instance. Called exactly once per environment:
 *
 *   - production / dev — `src/i18n/boot.ts` passes the full eleven-locale set
 *     from `./resources`, and `src/main.tsx` imports that module BEFORE
 *     `./App.tsx`, so init still runs before a single component module is
 *     evaluated and long before the first render. Identical to when this call
 *     sat at this module's top level.
 *   - tests — the `import.meta.env.MODE` branch at the foot of this file boots
 *     `baseResources` alone, so importing this module always yields a READY
 *     instance. Nine test files plus `src/__tests__/setup.ts` depend on that.
 *
 * The resource set is the only thing that varies. Options, plugins,
 * `supportedLngs`, the appliance hook and `availableLanguages` are identical in
 * both, and `./resources` remains the ONE list of which locales exist.
 *
 * Why the locale set is a parameter rather than an `import.meta.env` branch
 * over a `resources` literal here: a static `import` is evaluated whether or
 * not its binding reaches the literal — Vite's SSR transform hoists every
 * import to the head of the module and vite-node does not tree-shake — so a
 * branch alone would still pay all 4.0 MB of locale modules in each of the 229
 * test files. The saving comes from the ten heavy modules not being in THIS
 * module's import graph at all.
 *
 * Why they are not added after init instead: i18next's `setResolvedLanguage`
 * picks the first language that already holds translations AT INIT TIME, so a
 * de/ja user would resolve to `en` if their bundle arrived even a tick later.
 * Production must therefore hand the whole set to `init`, exactly as before.
 *
 * Plural suffixes: `compatibilityJSON` is deliberately left unset, so i18next
 * 25 runs JSON v4 and resolves `<key>_one` / `<key>_other` (via
 * Intl.PluralRules) and nothing else — a `<key>_plural` never resolves and
 * silently renders the singular at every count. `scripts/check-i18n-parity.mjs`
 * check 3 refuses that shape on disk; `src/__tests__/i18n/plurals.test.ts`
 * proves this instance still selects the right form at runtime.
 */
export function initI18n(resources: Resource): void {
  i18n
    .use(LanguageDetector)
    .use(initReactI18next)
    .init({
      resources,
      fallbackLng: 'en',
      supportedLngs: SUPPORTED_LNGS,

      detection: {
        // Order of detection methods
        order: ['localStorage', 'navigator', 'htmlTag'],
        // Key to use in localStorage
        lookupLocalStorage: 'bambutrack_language',
        // Cache user language
        caches: ['localStorage'],
      },

      interpolation: {
        escapeValue: false, // React already escapes
      },

      react: {
        useSuspense: false,
      },
    });

  applyApplianceLocale();
}

/**
 * Bambuddy Appliance hook: on the first SPA load after the firstboot wizard
 * runs, /api/v1/system/appliance returns the locale the user picked. We
 * apply it once (gated by a localStorage flag) and stop. On non-appliance
 * installs the endpoint either 404s or returns nulls — silent no-op.
 *
 * This runs AFTER i18n.init so the LanguageDetector has already populated a
 * default; we override that default exactly once for fresh appliances. The
 * appliance is then "consumed" and the language picker is the only way to
 * change locale going forward (the wizard ran once; future intent comes from
 * the running UI).
 */
function applyApplianceLocale() {
  if (typeof window === 'undefined' || !window.localStorage) return;
  const storage = window.localStorage;
  if (typeof storage.getItem !== 'function' || typeof storage.setItem !== 'function') return;
  if (storage.getItem(APPLIANCE_CONSUMED_KEY)) return;

  fetch('/api/v1/system/appliance')
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data || typeof data.locale !== 'string') return;
      if (!SUPPORTED_LNGS.includes(data.locale)) return;
      i18n.changeLanguage(data.locale);
      storage.setItem(APPLIANCE_CONSUMED_KEY, '1');
    })
    .catch(() => {
      // Endpoint absent or unreachable — non-appliance install or dev environment.
      // Leave the detector's choice in place.
    });
}

// The suite never loads `./boot`, so this module self-boots on the `en`
// baseline: importing it yields a ready instance in every environment. `MODE`
// is `'test'` under Vitest (it creates its Vite server with that mode; the same
// signal already gates the logging in `src/hooks/useWebSocket.ts`) and
// `'production'` in a `vite build`, where Vite inlines the literal and the
// bundler drops this branch. A test needing more than `en` registers it
// explicitly — see `src/__tests__/i18n/plurals.test.ts`.
if (import.meta.env.MODE === 'test') {
  initI18n(baseResources);
}

export default i18n;

// Helper to get available languages
export const availableLanguages = [
  { code: 'en', name: 'English', nativeName: 'English' },
  { code: 'de', name: 'German', nativeName: 'Deutsch' },
  { code: 'es', name: 'Spanish', nativeName: 'Español' },
  { code: 'fr', name: 'French', nativeName: 'Français' },
  { code: 'ja', name: 'Japanese', nativeName: '日本語' },
  { code: 'it', name: 'Italian', nativeName: 'Italiano' },
  { code: 'ko', name: 'Korean', nativeName: '한국어' },
  { code: 'pt-BR', name: 'Portuguese (Brazil)', nativeName: 'Português (Brasil)' },
  { code: 'zh-CN', name: 'Chinese (Simplified)', nativeName: '简体中文' },
  { code: 'zh-TW', name: 'Chinese (Traditional)', nativeName: '繁體中文' },
  { code: 'tr', name: 'Turkish', nativeName: 'Türkçe' },
];
