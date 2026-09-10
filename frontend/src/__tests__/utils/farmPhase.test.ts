/**
 * Phase derivation for the farm loop (Phase 4.3c): printing / cooling (release
 * target only) / awaiting plate clear / nothing. Pure function — the printer
 * card pill and the run-detail chips both consume it.
 */

import { describe, it, expect } from 'vitest';
import { deriveFarmPhase } from '../../utils/farmPhase';

describe('deriveFarmPhase', () => {
  it('reports printing while a job runs, even with a stale watch flag', () => {
    expect(
      deriveFarmPhase({ state: 'RUNNING', awaiting_plate_clear: false, eject_watch: null }),
    ).toEqual({ kind: 'printing' });
    // RUNNING wins over anything else — the in-file sweep runs inside the job.
    expect(
      deriveFarmPhase({ state: 'RUNNING', awaiting_plate_clear: true, eject_watch: { threshold_c: 33 } }),
    ).toEqual({ kind: 'printing' });
  });

  it('reports cooling with the watch threshold while the watch is armed', () => {
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33 },
      }),
    ).toEqual({ kind: 'cooling', threshold: 33, held: false });
  });

  it('reports a HELD plate when the watch carries a hold_z', () => {
    // hold_z means the plate is parked at the nozzle plane for the whole wait —
    // the operator must not jog the toolhead until the eject runs.
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: 2 },
      }),
    ).toEqual({ kind: 'cooling', threshold: 33, held: true });
    // A hold at Z0 is still a hold — only a non-finite/absent value is "not held".
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: 0 },
      }),
    ).toEqual({ kind: 'cooling', threshold: 33, held: true });
  });

  it('is not held when hold_z is null, absent, or non-finite', () => {
    const notHeld = { kind: 'cooling', threshold: 33, held: false };
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: null },
      }),
    ).toEqual(notHeld);
    // Older status payloads carry no hold_z at all.
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33 },
      }),
    ).toEqual(notHeld);
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: Number.NaN },
      }),
    ).toEqual(notHeld);
  });

  it('reports awaiting plate clear when the gate is raised with NO watch', () => {
    expect(
      deriveFarmPhase({ state: 'FINISH', awaiting_plate_clear: true, eject_watch: null }),
    ).toEqual({ kind: 'awaitingPlateClear' });
    // undefined eject_watch (older status payloads) behaves the same.
    expect(deriveFarmPhase({ state: 'FAILED', awaiting_plate_clear: true })).toEqual({
      kind: 'awaitingPlateClear',
    });
  });

  it('returns null when nothing farm-phase-worthy is happening', () => {
    expect(deriveFarmPhase({ state: 'IDLE', awaiting_plate_clear: false, eject_watch: null })).toBeNull();
    expect(deriveFarmPhase({ state: null })).toBeNull();
  });

  it('ignores a non-numeric threshold defensively', () => {
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: Number.NaN },
      }),
    ).toEqual({ kind: 'awaitingPlateClear' });
  });
});
