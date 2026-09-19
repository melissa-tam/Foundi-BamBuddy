/**
 * "Which printers won't take units from this run yet, and why" — the run's
 * eligibility panel. ONE implementation for both surfaces that show it: the run
 * detail page (its own card, under the page's h1) and a run card on the runs
 * list (chrome-less, inside a card whose title is an h3).
 *
 * The two props exist because those surfaces differ in exactly two ways and in
 * nothing else: `headingLevel` keeps the document outline valid on each page,
 * and `chrome` keeps a Card out of a Card.
 *
 * The reason SET lives in `utils/productionRuns.eligibilityReasons` — this
 * module owns only its copy — so `hasLiveBlockedPrinters` can gate the chip on
 * exactly what this panel would print, without needing a translator.
 */
import { useTranslation } from 'react-i18next';
import { AlertTriangle } from 'lucide-react';
import { Card, CardContent } from './Card';
import { eligibilityReasons, type EligibilityReason } from '../utils/productionRuns';
import type { RunPrinterState } from '../types/productionRuns';

/** Locale leaf per reason kind; `capability` has none — it IS a sentence. */
const REASON_KEYS: Record<Exclude<EligibilityReason['kind'], 'capability'>, string> = {
  offline: 'productionRuns.detail.eligibility.offline',
  quarantined: 'productionRuns.detail.eligibility.quarantined',
  awaitingPlateClear: 'productionRuns.detail.eligibility.awaitingPlateClear',
  modelMismatch: 'productionRuns.detail.eligibility.modelMismatch',
  filamentShort: 'productionRuns.detail.eligibility.filamentShort',
  noUsbDrive: 'productionRuns.detail.eligibility.noUsbDrive',
};

/**
 * One reason as a short display line. `capability_reason` and
 * `filament_short_detail` are backend-authored human sentences and render
 * verbatim; the rest is a label, optionally suffixed with its detail.
 */
function reasonText(reason: EligibilityReason, t: (k: string) => string): string {
  if (reason.kind === 'capability') return reason.detail;
  const label = t(REASON_KEYS[reason.kind]);
  if (reason.kind === 'modelMismatch' || reason.kind === 'filamentShort') {
    return reason.detail ? `${label} — ${reason.detail}` : label;
  }
  return label;
}

export interface NotEligibleBannerProps {
  printerStates: RunPrinterState[];
  /**
   * Heading level for the panel title. 2 on the run detail page (a top-level
   * section under the page heading); 4 inside a run card, whose own title is an
   * h3 — an h2 there would break the outline.
   */
  headingLevel: 2 | 4;
  /**
   * 'card' wraps the banner in the page's Card chrome (the detail page's own
   * section); 'inline' renders the bare banner for a caller that is already
   * inside a Card. No Card-in-Card.
   */
  chrome: 'card' | 'inline';
}

/**
 * Banner listing every printer the run targets that won't participate yet, with
 * each printer's blocking reasons. Renders nothing when every printer is
 * eligible (no empty-state card), so a clean run adds no height. Mirrors the
 * RunStagedBanner tone/styling and reuses the chips' red "blocked" palette; the
 * caller's query (detail poll, or the list's `production_run_changed` WS
 * invalidation) keeps it live, so a resolved printer drops off on the next fetch.
 */
export function NotEligibleBanner({ printerStates, headingLevel, chrome }: NotEligibleBannerProps) {
  const { t } = useTranslation();
  const ineligible = printerStates
    .map((state) => ({ state, reasons: eligibilityReasons(state).map((r) => reasonText(r, t)) }))
    .filter((entry) => entry.reasons.length > 0);

  if (ineligible.length === 0) return null;

  const Heading = `h${headingLevel}` as 'h2' | 'h4';

  const banner = (
    <div className="flex items-start gap-2 rounded-lg border border-red-500/40 bg-red-500/10 p-3">
      <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-300" aria-hidden="true" />
      <div className="min-w-0">
        <Heading className="text-sm font-semibold text-red-200">
          {t('productionRuns.detail.eligibility.title')}
        </Heading>
        <p className="mt-0.5 text-xs text-red-300/90">
          {t('productionRuns.detail.eligibility.description')}
        </p>
        <ul className="mt-2 space-y-2">
          {ineligible.map(({ state, reasons }) => (
            <li key={state.printer_id}>
              <span className="text-sm font-medium text-white">{state.name}</span>
              <ul className="mt-0.5 space-y-0.5">
                {reasons.map((reason) => (
                  <li key={reason} className="text-xs text-red-300">
                    {reason}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );

  if (chrome === 'inline') return banner;

  return (
    <Card>
      <CardContent>{banner}</CardContent>
    </Card>
  );
}
