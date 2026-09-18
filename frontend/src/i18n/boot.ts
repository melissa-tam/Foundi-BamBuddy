import { initI18n } from './index';
import { allResources } from './resources';

/**
 * Production/dev i18n bootstrap: initialise the instance with every locale.
 *
 * `src/main.tsx` imports THIS module, not `./index`, and imports it before
 * `./App.tsx`. ES modules evaluate a module's dependencies in source order
 * before its own body, so `./index` and `./resources` are both fully evaluated
 * and `initI18n` has run by the time App's component graph starts loading —
 * the same ordering the app had when `init` sat at the top level of `./index`.
 *
 * That ordering is the reason this is a module and not two lines in `main.tsx`:
 * `import` declarations hoist, so a bare `initI18n(allResources)` call in
 * `main.tsx`'s body would run AFTER App's entire module graph had evaluated.
 */
initI18n(allResources);
