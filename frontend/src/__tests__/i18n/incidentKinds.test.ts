/**
 * The printer-card incident chip looks its label up DYNAMICALLY —
 * `t(`printers.incident.${status.open_incident.kind}`)` (PrintersPage) — so the
 * compiler cannot see the key. A backend `printer_incident` kind that ships
 * without its locale leaf renders the raw key string on the operator's card,
 * on the one surface a FOREIGN print's hold is visible at all.
 *
 * This pins the two halves together: the Record makes the kind list exhaustive
 * against the API union (a kind added to `client.ts` without a row here fails
 * `tsc -b`), and the assertion makes each kind carry English copy. The other
 * ten locales are covered by the parity gate (`npm run check:i18n`), which
 * requires an identical leaf set.
 */
import { describe, it, expect } from 'vitest';
import type { PrinterStatus } from '../../api/client';
import en from '../../i18n/locales/en';

type IncidentKind = NonNullable<PrinterStatus['open_incident']>['kind'];

/** Every kind the status payload can carry — value is unused; the KEYS are the
 *  assertion, and `Record` makes omitting one a compile error. */
const INCIDENT_KINDS: Record<IncidentKind, true> = {
  jam: true,
  runout: true,
  physical: true,
  // The 2026-09-04 pause-recovery wave's three pause causes.
  power_loss: true,
  plate_vision: true,
  z_reference_lost: true,
};

describe('printer incident chip labels', () => {
  const labels: Record<string, string> = en.printers.incident;

  it('has English copy for every incident kind the API can send', () => {
    for (const kind of Object.keys(INCIDENT_KINDS)) {
      expect(labels[kind], `missing printers.incident.${kind}`).toBeTruthy();
    }
  });

  it('has the in-progress label the chip shows while an incident is still recovering', () => {
    expect(labels.recovering).toBeTruthy();
  });
});
