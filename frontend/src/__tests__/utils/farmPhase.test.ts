/**
 * Phase derivation for the farm loop (Phase 4.3c): printing / eject deferred /
 * cooling (eject line, or none) / awaiting plate clear / nothing. Pure function
 * — the printer card pill and the run-detail chips both consume it, and both
 * word a cooling watch through `coolingLabel`.
 */

import { describe, it, expect } from 'vitest';
import i18n from '../../i18n';
import { coolingLabel, deriveFarmPhase } from '../../utils/farmPhase';

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

  // A watch armed while shop air was unknown quotes no line: the plate still
  // cools and releases on its own air or at its plateau. The watch's presence
  // is the cooling fact — a missing line never demotes it to "awaiting clear".
  it('reports cooling with no line when the watch armed without one', () => {
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: null },
      }),
    ).toEqual({ kind: 'cooling', threshold: null, held: false });
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: null, hold_z: 2 },
      }),
    ).toEqual({ kind: 'cooling', threshold: null, held: true });
  });

  it('still reports a deferred eject when the watch has no line', () => {
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: null, deferred: true },
      }),
    ).toEqual({ kind: 'ejectDeferred', held: false });
  });

  it('treats a non-finite line as no line, never as a number', () => {
    expect(
      deriveFarmPhase({
        state: 'FINISH',
        awaiting_plate_clear: true,
        eject_watch: { threshold_c: Number.NaN },
      }),
    ).toEqual({ kind: 'cooling', threshold: null, held: false });
  });
});

describe('coolingLabel', () => {
  it('quotes the rounded eject line when the watch has one', () => {
    expect(coolingLabel({ kind: 'cooling', threshold: 27.6, held: false })).toEqual({
      key: 'printers.phase.cooling',
      threshold: 28,
    });
    expect(coolingLabel({ kind: 'cooling', threshold: 27.4, held: true })).toEqual({
      key: 'printers.phase.coolingHeld',
      threshold: 27,
    });
  });

  it('names the plateau when the watch has no line', () => {
    expect(coolingLabel({ kind: 'cooling', threshold: null, held: false })).toEqual({
      key: 'printers.phase.coolingToPlateau',
      threshold: null,
    });
    expect(coolingLabel({ kind: 'cooling', threshold: null, held: true })).toEqual({
      key: 'printers.phase.coolingToPlateauHeld',
      threshold: null,
    });
  });

  it('resolves every key it can return in the live locale', () => {
    // A key that fell out of the locale would render as the raw key string.
    for (const phase of [
      { kind: 'cooling' as const, threshold: 30, held: false },
      { kind: 'cooling' as const, threshold: 30, held: true },
      { kind: 'cooling' as const, threshold: null, held: false },
      { kind: 'cooling' as const, threshold: null, held: true },
    ]) {
      const label = coolingLabel(phase);
      expect(i18n.exists(label.key)).toBe(true);
    }
  });
});
