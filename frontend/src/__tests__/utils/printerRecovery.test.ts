/**
 * `recoverEffects` — the ONE answer to "does Recover apply here, and what will
 * it do". Four surfaces read it (card menu, quarantine banner, stalled-eject
 * banner, confirm dialog), so these pin the whole vocabulary rather than the
 * shape of any one of them.
 *
 * The case that started the wave: printer 011-H2S sat with an escalated
 * `physical` row, no quarantine, no plate gate, no lease and no eject — every
 * pre-util predicate said "nothing to recover" while the backend verb was the
 * only thing that could close the row. `equipment_fault` therefore comes from
 * the BACKEND's `operator_exits`, never from the incident kind.
 */
import { describe, it, expect } from 'vitest';
import type { Printer, PrinterIncidentKind, PrinterStatus } from '../../api/client';
import {
  RECOVER_EFFECTS,
  inFlightEject,
  recoverApplies,
  recoverEffects,
} from '../../utils/printerRecovery';

function createPrinter(overrides: Partial<Printer> = {}): Printer {
  return {
    id: 9,
    name: '011-H2S',
    model: 'H2S',
    quarantined: false,
    quarantine_reason: null,
    ...overrides,
  } as Printer;
}

function createStatus(overrides: Partial<PrinterStatus> = {}): PrinterStatus {
  return {
    id: 9,
    name: '011-H2S',
    connected: true,
    state: 'IDLE',
    awaiting_plate_clear: false,
    ...overrides,
  } as PrinterStatus;
}

/** An occupancy record with nothing held — each test raises one thing. */
function occupancy(overrides: Partial<NonNullable<PrinterStatus['occupancy']>> = {}) {
  return {
    plate: { occupied: false, source_subtask_id: null, policy: null, since: null, refusal: null },
    eject: null,
    lease_age_s: null,
    ...overrides,
  };
}

function ejectClaim(runtimeExceeded = false) {
  return {
    purpose: 'production',
    started: true,
    age_s: 154,
    hydrated: false,
    runtime_exceeded: runtimeExceeded,
  };
}

function incident(
  kind: PrinterIncidentKind,
  operatorExits: boolean,
): NonNullable<PrinterStatus['open_incident']> {
  return {
    id: 188,
    kind,
    status: 'escalated',
    slot_desc: null,
    created_at: '2026-09-17T09:43:00Z',
    operator_exits: operatorExits,
    printer_messages: [],
    driver_live: false,
  };
}

describe('recoverEffects', () => {
  it('reports nothing on a printer the farm holds nothing on', () => {
    const effects = recoverEffects(createPrinter(), createStatus({ occupancy: occupancy() }));

    expect(effects).toEqual([]);
    expect(recoverApplies(effects)).toBe(false);
  });

  it('reports nothing when no status frame has landed yet', () => {
    expect(recoverEffects(createPrinter(), undefined)).toEqual([]);
  });

  it('reports the raised plate gate', () => {
    const status = createStatus({
      occupancy: occupancy({
        plate: { occupied: true, source_subtask_id: '783388626', policy: 'CooldownEject', since: null, refusal: null },
      }),
    });

    expect(recoverEffects(createPrinter(), status)).toEqual(['plate']);
  });

  it('reports a held dispatch lease, including an age of 0 s', () => {
    expect(
      recoverEffects(createPrinter(), createStatus({ occupancy: occupancy({ lease_age_s: 0 }) })),
    ).toEqual(['lease']);
  });

  it('reports an in-flight eject claim, owned or stalled', () => {
    const owned = createStatus({ occupancy: occupancy({ eject: ejectClaim() }) });
    const stalled = createStatus({ occupancy: occupancy({ eject: ejectClaim(true) }) });

    expect(recoverEffects(createPrinter(), owned)).toEqual(['eject']);
    expect(recoverEffects(createPrinter(), stalled)).toEqual(['eject']);
  });

  it('reports the quarantine from the fleet row, with no status frame needed', () => {
    expect(recoverEffects(createPrinter({ quarantined: true }), undefined)).toEqual(['quarantine']);
  });

  it('reports an equipment fault the operator verb exits — no gate of any kind', () => {
    // A z_reference_lost hold raises nothing the authority owns; Recover is
    // still its sanctioned exit, and before the util the card offered nothing.
    const status = createStatus({
      occupancy: occupancy(),
      open_incident: incident('z_reference_lost', true),
    });

    const effects = recoverEffects(createPrinter(), status);
    expect(effects).toEqual(['equipment_fault']);
    expect(recoverApplies(effects)).toBe(true);
  });

  it('does NOT report a fault that waits for the wire, not for the operator', () => {
    // A runout closes on the AMS reading filament again; Recover is not its exit.
    const status = createStatus({
      occupancy: occupancy(),
      open_incident: incident('runout', false),
    });

    expect(recoverEffects(createPrinter(), status)).toEqual([]);
  });

  it('never infers the fault effect from the kind — only from operator_exits', () => {
    const exits = createStatus({ occupancy: occupancy(), open_incident: incident('physical', true) });
    const stands = createStatus({ occupancy: occupancy(), open_incident: incident('physical', false) });

    expect(recoverEffects(createPrinter(), exits)).toEqual(['equipment_fault']);
    expect(recoverEffects(createPrinter(), stands)).toEqual([]);
  });

  it('reports several effects at once, in RECOVER_EFFECTS order', () => {
    const status = createStatus({
      occupancy: occupancy({
        plate: { occupied: true, source_subtask_id: '1', policy: null, since: null, refusal: null },
        eject: ejectClaim(true),
        lease_age_s: 42,
      }),
      open_incident: incident('physical', true),
    });

    const effects = recoverEffects(createPrinter({ quarantined: true }), status);

    expect(effects).toEqual([...RECOVER_EFFECTS]);
    expect(recoverApplies(effects)).toBe(true);
  });
});

describe('inFlightEject', () => {
  it('hands back the claim the eject effect keys on, watchdog verdict included', () => {
    const status = createStatus({ occupancy: occupancy({ eject: ejectClaim(true) }) });

    expect(inFlightEject(status)?.runtime_exceeded).toBe(true);
    expect(inFlightEject(status)?.age_s).toBe(154);
  });

  it('is null with no claim, no occupancy record and no status at all', () => {
    expect(inFlightEject(createStatus({ occupancy: occupancy() }))).toBeNull();
    expect(inFlightEject(createStatus())).toBeNull();
    expect(inFlightEject(undefined)).toBeNull();
  });
});
