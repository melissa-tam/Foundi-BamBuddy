/**
 * `utils/incidents` — the live held time, the cleared-with-no-person share and
 * the resolve-source label fallback.
 */
import { describe, expect, it } from 'vitest';
import {
  INCIDENT_OUTCOMES,
  NO_RESOLVE_SOURCE_LABEL_KEY,
  clearedNoPersonShare,
  isOpenOutcome,
  liveHeldSeconds,
  resolveSourceLabelKey,
} from '../../utils/incidents';

const RESPONDED_AT = 1_000_000;

describe('liveHeldSeconds', () => {
  it('advances an open row by the time since the response landed', () => {
    expect(liveHeldSeconds({ held_s: 60, outcome: 'held' }, RESPONDED_AT, RESPONDED_AT + 5_000)).toBe(65);
    expect(liveHeldSeconds({ held_s: 60, outcome: 'recovering' }, RESPONDED_AT, RESPONDED_AT + 1_500)).toBe(61.5);
  });

  it('never moves a closed row', () => {
    for (const outcome of INCIDENT_OUTCOMES.filter((value) => !isOpenOutcome(value))) {
      expect(liveHeldSeconds({ held_s: 60, outcome }, RESPONDED_AT, RESPONDED_AT + 60_000)).toBe(60);
    }
  });

  it('adds nothing when the clock reads earlier than the response', () => {
    expect(liveHeldSeconds({ held_s: 60, outcome: 'held' }, RESPONDED_AT, RESPONDED_AT - 10_000)).toBe(60);
  });
});

describe('clearedNoPersonShare', () => {
  it('is zero_human over total', () => {
    expect(clearedNoPersonShare({ total: 55, zero_human: 11 })).toBeCloseTo(0.2);
  });

  it('is null when there were no faults', () => {
    expect(clearedNoPersonShare({ total: 0, zero_human: 0 })).toBeNull();
  });
});

describe('resolveSourceLabelKey', () => {
  it('names a known token by its leaf', () => {
    expect(resolveSourceLabelKey('driver_swap')).toEqual({ key: 'incidents.closedBy.driver_swap' });
  });

  it('reads no source as the none leaf', () => {
    expect(resolveSourceLabelKey(null)).toEqual({ key: NO_RESOLVE_SOURCE_LABEL_KEY });
  });

  it('falls back to the raw token the client does not know', () => {
    expect(resolveSourceLabelKey('brand_new_closer')).toEqual({ raw: 'brand_new_closer' });
    // An inherited Object property is not a token.
    expect(resolveSourceLabelKey('toString')).toEqual({ raw: 'toString' });
  });
});
