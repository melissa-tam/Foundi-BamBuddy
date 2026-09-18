import type { Resource } from 'i18next';

// Import translations directly for bundling
import en from './locales/en';
import de from './locales/de';
import es from './locales/es';
import fr from './locales/fr';
import ja from './locales/ja';
import it from './locales/it';
import ko from './locales/ko';
import ptBR from './locales/pt-BR';
import zhCN from './locales/zh-CN';
import zhTW from './locales/zh-TW';
import tr from './locales/tr';

/**
 * THE list of locales the app ships, and the only place a locale module is
 * named. `src/i18n/boot.ts` hands this to `initI18n` for production and dev;
 * `src/__tests__/i18n/plurals.test.ts` registers it into the live instance for
 * the one test that resolves through every locale.
 *
 * This module is deliberately NOT imported by `src/i18n/index.ts`: these eleven
 * files are 4.0 MB / ~82k lines on disk, and a static import is evaluated in
 * every one of the 229 test files whether or not the test renders a translated
 * string. Keeping them out of the instance module's import graph is the whole
 * point of the split — see the statement on `initI18n` in `./index.ts`. Adding
 * an import of this module there would silently undo it;
 * `src/__tests__/i18n/bootstrap.test.ts` fails if that happens.
 *
 * Key order matches the eleven entries of `availableLanguages` in `./index.ts`
 * and the `SUPPORTED_LNGS` list it inits with.
 */
export const allResources: Resource = {
  en: { translation: en },
  de: { translation: de },
  es: { translation: es },
  fr: { translation: fr },
  ja: { translation: ja },
  it: { translation: it },
  ko: { translation: ko },
  'pt-BR': { translation: ptBR },
  'zh-CN': { translation: zhCN },
  'zh-TW': { translation: zhTW },
  tr: { translation: tr },
};
