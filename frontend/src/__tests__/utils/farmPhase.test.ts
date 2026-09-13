/**
 * Phase derivation for the farm loop (Phase 4.3c): printing / eject deferred /
 * cooling (release target only) / awaiting plate clear / nothing. Pure function
 * — the printer card pill and the run-detail chips both consume it.
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

  it('reports a deferred eject while the watch withholds the sweep under a hold', () => {
    // The watch keeps reporting its threshold after the bed reaches it — the
    // cooling is over, the fans are retired, and only the hold stands between
    // the plate and the sweep. `deferred` is the only fact that says so.
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, deferred: true },
      }),
    ).toEqual({ kind: 'ejectDeferred', held: false });
    // No hold_z at all (an older payload, or a plate the farm never raised).
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: null, deferred: true },
      }),
    ).toEqual({ kind: 'ejectDeferred', held: false });
  });

  it('carries the HELD plate claim into a deferred eject', () => {
    // The plate can still be parked at the nozzle plane while the sweep waits on
    // the hold — the do-not-jog constraint outlives the cooldown, so `held` is
    // derived the same way it is for cooling.
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: 2, deferred: true },
      }),
    ).toEqual({ kind: 'ejectDeferred', held: true });
    // A hold at Z0 is still a hold, as on the cooling branch.
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, hold_z: 0, deferred: true },
      }),
    ).toEqual({ kind: 'ejectDeferred', held: true });
  });

  it('reports printing, not a deferred eject, while a job runs', () => {
    // A hold does not stop a running job; RUNNING still wins.
    expect(
      deriveFarmPhase({
        state: 'RUNNING',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, deferred: true },
      }),
    ).toEqual({ kind: 'printing' });
  });

  it('reports cooling when the watch is armed but nothing is deferred', () => {
    const cooling = { kind: 'cooling', threshold: 33, held: false };
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33, deferred: false },
      }),
    ).toEqual(cooling);
    // Older status payloads carry no `deferred` at all.
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: 33 },
      }),
    ).toEqual(cooling);
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
