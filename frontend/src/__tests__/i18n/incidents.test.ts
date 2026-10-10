/**
 * The Faults tab looks its outcome and "closed by" labels up DYNAMICALLY
 * (`utils/incidents.outcomeLabelKey` / `resolveSourceLabelKey`), so the
 * compiler cannot see the keys. The Records below are exhaustive against the
 * unions (a token added to `types/incidents.ts` without a row here fails
 * `tsc`), and each must carry English copy. The other ten locales are held to
 * the same leaf set by the parity gate (`npm run check:i18n`).
 */
import { describe, expect, it } from 'vitest';
import en from '../../i18n/locales/en';
import type { IncidentOutcome, IncidentResolveSource } from '../../types/incidents';
import {
  INCIDENT_OUTCOMES,
  NO_RESOLVE_SOURCE_LABEL_KEY,
  RESOLVE_SOURCE_LABEL_KEY,
  outcomeLabelKey,
} from '../../utils/incidents';

const OUTCOMES: Record<IncidentOutcome, true> = {
  recovering: true,
  held: true,
  auto_recovered: true,
  human_resolved: true,
  resolved_unpaged: true,
  taken_over: true,
  transient: true,
};

const RESOLVE_SOURCES: Record<IncidentResolveSource, true> = {
  auto_resume: true,
  observed_running: true,
  terminal: true,
  operator: true,
  paused_elsewhere: true,
  wire_clear: true,
  repair_observed: true,
  repair_completed: true,
  driver_swap: true,
  driver_self_heal: true,
  driver_restart: true,
  refill_resumed: true,
  startup_rearm: true,
  recheck_passed: true,
  plate_refused: true,
  handed_over: true,
  job_ended_unseen: true,
  driver_ended: true,
  legacy_vision_stop: true,
};

/** Resolve a dotted key against the English bundle. */
function leaf(key: string): unknown {
  return key.split('.').reduce<unknown>((node, part) => {
    if (node === null || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, en);
}

describe('incident ledger labels', () => {
  it('lists every outcome, in the backend order', () => {
    expect([...INCIDENT_OUTCOMES].sort()).toEqual(Object.keys(OUTCOMES).sort());
  });

  it('has English copy for every outcome', () => {
    for (const outcome of Object.keys(OUTCOMES) as IncidentOutcome[]) {
      expect(leaf(outcomeLabelKey(outcome)), `missing ${outcomeLabelKey(outcome)}`).toEqual(expect.any(String));
    }
  });

  it('has English copy for every resolve source and for none', () => {
    for (const source of Object.keys(RESOLVE_SOURCES) as IncidentResolveSource[]) {
      const key = RESOLVE_SOURCE_LABEL_KEY[source];
      expect(leaf(key), `missing ${key}`).toEqual(expect.any(String));
    }
    expect(leaf(NO_RESOLVE_SOURCE_LABEL_KEY)).toEqual(expect.any(String));
  });

  it('carries no closed-by copy for a token the union does not name', () => {
    const known = new Set<string>([...Object.keys(RESOLVE_SOURCES), 'none']);
    for (const key of Object.keys(en.incidents.closedBy)) {
      expect(known.has(key), `incidents.closedBy.${key} names no resolve source`).toBe(true);
    }
  });
});
