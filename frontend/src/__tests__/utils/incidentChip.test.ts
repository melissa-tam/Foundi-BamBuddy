/**
 * `utils/incidentChip.holdChip` — WHICH hold the printer card's chip names, in
 * which tone, with which tooltip sentences. The precedence pinned here, first
 * match wins:
 *
 *   1. the farm's refill state (`toolhead.refill`) → the toolhead variant,
 *      over ANY incident row (a failed refill under a jam row names the slot);
 *   2. the open incident row (own-surface kinds skipped) → the incident variant,
 *      so a plate check / power-loss pause on an empty toolhead keeps its own chip;
 *   3. a paused print on an empty toolhead with nothing open → the toolhead variant,
 *      ONLY for the backend verdicts a person should see (`refill_reason`).
 *
 * Every key the model emits must resolve in `en`, so a chip never prints a raw key.
 */
import { describe, expect, it } from 'vitest';
import type { OpenIncidentState, PrinterIncidentKind, ToolheadRefillReason, ToolheadState } from '../../api/client';
import en from '../../i18n/locales/en';
import { holdChip, type HoldChipStatus } from '../../utils/incidentChip';

function incident(
  kind: PrinterIncidentKind,
  status: OpenIncidentState['status'],
  slotDesc: string | null = null,
): OpenIncidentState {
  return {
    id: 1,
    kind,
    status,
    driver_live: false,
    slot_desc: slotDesc,
    created_at: null,
    operator_exits: false,
    printer_messages: [],
  };
}

/** An empty toolhead with NO verdict field — the shape an older backend sends. */
const EMPTY: ToolheadState = { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: null };
/** An empty toolhead carrying the backend's T3 verdict. */
const emptyWith = (refillReason: ToolheadRefillReason | null): ToolheadState => ({
  feed: 'empty',
  active_tray: null,
  was_feeding_tray: null,
  refill: null,
  refill_reason: refillReason,
});
/** The verdicts rule (3) speaks for — the hold a person should see. */
const SPOKEN_REASONS = ['owed', 'maintenance', 'physical', 'command_pending'] as const satisfies readonly ToolheadRefillReason[];
/** Every other verdict: the printer feeds itself, nothing is wrong, or nothing is known. */
const SILENT_REASONS = [
  'fed',
  'runout_demand',
  'power_loss_prompt',
  'before_first_layer',
  'last_layer',
  'change_in_flight',
  'eject_sweep',
  'unknown',
] as const satisfies readonly ToolheadRefillReason[];
const FED: ToolheadState = { feed: 'fed', active_tray: 2, was_feeding_tray: null, refill: null };
const LOADING: ToolheadState = { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'loading', slot: 'AMS A slot 1', answer: null } };
const FAILED: ToolheadState = {
  feed: 'empty',
  active_tray: null,
  was_feeding_tray: null,
  refill: { phase: 'failed', slot: 'AMS A slot 1', answer: 'no_movement' },
};

function status(overrides: Partial<HoldChipStatus>): HoldChipStatus {
  return { state: 'PAUSE', open_incident: null, toolhead: null, ...overrides };
}

function enValue(key: string): unknown {
  return key.split('.').reduce<unknown>(
    (node, part) => (node !== null && typeof node === 'object' ? (node as Record<string, unknown>)[part] : undefined),
    en,
  );
}

const keysOf = (chip: ReturnType<typeof holdChip>) => chip?.tooltip.map((copy) => copy.key);

describe('holdChip precedence', () => {
  it('names a failed refill over the jam row it ran under', () => {
    const chip = holdChip(status({ open_incident: incident('jam', 'escalated', 'AMS A slot 3'), toolhead: FAILED }));
    expect(chip?.variant).toBe('toolhead');
    expect(chip?.tone).toBe('held');
    expect(chip?.label.key).toBe('printers.incident.toolhead_refill');
    expect(keysOf(chip)).toEqual([
      'printers.toolhead.loadFailed',
      'printers.toolhead.answer.no_movement',
      'printers.incidentAction.toolhead_refill',
    ]);
    expect(chip?.tooltip[0]?.values).toEqual({ slot: 'AMS A slot 1' });
    // The jam row's slot qualifier belongs to the jam chip, not to this one.
    expect(chip?.qualifier).toBeNull();
  });

  it.each(['jam', 'physical', 'runout'] as const)('names a farm load in flight over a %s row', (kind) => {
    const chip = holdChip(status({ open_incident: incident(kind, 'recovering'), toolhead: LOADING }));
    expect(chip?.variant).toBe('toolhead');
    expect(chip?.tone).toBe('acting');
    expect(keysOf(chip)).toEqual(['printers.toolhead.loading']);
  });

  it.each(['plate_vision', 'power_loss', 'z_reference_lost', 'jam', 'runout', 'physical'] as const)(
    'keeps the %s row on an empty toolhead with no refill state, even when a resume is owed',
    (kind) => {
      for (const toolhead of [EMPTY, emptyWith('owed')]) {
        const chip = holdChip(status({ open_incident: incident(kind, 'escalated'), toolhead }));
        expect(chip?.variant).toBe('incident');
        expect(chip?.label.key).toBe(`printers.incident.${kind}`);
      }
    },
  );

  it('shows nothing for a maintenance hold on a fed toolhead', () => {
    expect(holdChip(status({ open_incident: incident('service_hold', 'escalated'), toolhead: FED }))).toBeNull();
  });

  it('shows nothing for an empty toolhead that is not paused, whatever the verdict', () => {
    expect(holdChip(status({ state: 'IDLE', toolhead: emptyWith('owed') }))).toBeNull();
    expect(holdChip(status({ state: 'RUNNING', toolhead: emptyWith('owed') }))).toBeNull();
  });

  it('shows nothing for a paused print on a fed or unread toolhead', () => {
    expect(holdChip(status({ toolhead: FED }))).toBeNull();
    expect(holdChip(status({ toolhead: { feed: 'unknown', active_tray: null, was_feeding_tray: null, refill: null } }))).toBeNull();
    expect(holdChip(status({ toolhead: null }))).toBeNull();
  });
});

/*
 * Rule (3) — a paused print on an empty toolhead with nothing open — speaks ONLY
 * for the backend's T3 verdict (`toolhead.refill_reason`, what a Resume would do
 * now), never a client re-derivation. The defect it closes: 012-H2S 2026-10-10,
 * PAUSEd at layer 0 at the plate-marker dialog with `tray_now` 255 and no row,
 * read red "Toolhead empty · Load a slot, then resume." — before the first layer
 * the printer loads filament itself.
 */
describe('holdChip rule (3): the resume verdict', () => {
  it('shows no chip for the 012-H2S shape: paused before the first layer', () => {
    expect(holdChip(status({ toolhead: emptyWith('before_first_layer') }))).toBeNull();
  });

  it.each(SILENT_REASONS)('shows no chip for a %s verdict', (reason) => {
    expect(holdChip(status({ toolhead: emptyWith(reason) }))).toBeNull();
  });

  it('shows no chip with no verdict, or on an older backend that sends none', () => {
    expect(holdChip(status({ toolhead: emptyWith(null) }))).toBeNull();
    expect(holdChip(status({ toolhead: EMPTY }))).toBeNull();
  });

  it('shows no chip for a verdict this build does not know', () => {
    const newer = emptyWith('a_newer_reason' as ToolheadRefillReason);
    expect(holdChip(status({ toolhead: newer }))).toBeNull();
  });

  it.each(SPOKEN_REASONS)('shows red "Toolhead empty" for a %s verdict with its own tooltip', (reason) => {
    const chip = holdChip(status({ toolhead: emptyWith(reason) }));
    expect(chip?.variant).toBe('toolhead');
    expect(chip?.tone).toBe('held');
    expect(chip?.label.key).toBe('printers.incident.toolhead_refill');
    expect(keysOf(chip)).toEqual([`printers.toolhead.reason.${reason}`]);
  });

  it('speaks the maintenance verdict past a maintenance-banner row, which never takes the chip', () => {
    const chip = holdChip(
      status({ open_incident: incident('service_hold', 'escalated'), toolhead: emptyWith('maintenance') }),
    );
    expect(keysOf(chip)).toEqual(['printers.toolhead.reason.maintenance']);
  });

  it('leaves rule (1) to the refill state, whatever the verdict says', () => {
    const chip = holdChip(status({ toolhead: { ...LOADING, refill_reason: 'before_first_layer' } }));
    expect(keysOf(chip)).toEqual(['printers.toolhead.loading']);
  });
});

describe('holdChip states', () => {
  it('names an escalated refill hold "Toolhead empty" with the load-a-slot exit', () => {
    const chip = holdChip(status({ open_incident: incident('toolhead_refill', 'escalated'), toolhead: EMPTY }));
    expect(chip?.variant).toBe('incident');
    expect(chip?.tone).toBe('held');
    expect(chip?.label.key).toBe('printers.incident.toolhead_refill');
    expect(keysOf(chip)).toEqual(['printers.incidentAction.toolhead_refill']);
  });

  it('names no person exit on a refill hold the farm still owns', () => {
    const chip = holdChip(status({ open_incident: incident('toolhead_refill', 'recovering'), toolhead: FED }));
    expect(chip?.tone).toBe('acting');
    expect(chip?.label.key).toBe('printers.incident.recovering');
    expect(keysOf(chip)).toEqual(['printers.incidentRecoveringAction.toolhead_refill']);
  });

  it('says "a spool" when the load names no slot', () => {
    const chip = holdChip(
      status({ toolhead: { feed: 'unknown', active_tray: null, was_feeding_tray: null, refill: { phase: 'loading', slot: null, answer: null } } }),
    );
    expect(keysOf(chip)).toEqual(['printers.toolhead.loadingAnySlot']);
  });

  it('leaves out the AMS answer when the failed load carries none', () => {
    const chip = holdChip(status({ toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot: null, answer: null } } }));
    expect(keysOf(chip)).toEqual(['printers.toolhead.loadFailedAnySlot', 'printers.incidentAction.toolhead_refill']);
  });

  it('names an AMS that moved without finishing the load', () => {
    const chip = holdChip(
      status({ toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot: 'AMS B slot 2', answer: 'acted' } } }),
    );
    expect(keysOf(chip)).toContain('printers.toolhead.answer.loadActed');
  });

  /*
   * `command` names the step that failed. A failed refill UNLOAD reads as an
   * unload — never "Load failed" — and `acted` names the step too; the AMS
   * answer for no movement and the person's exit are the same for both.
   */
  it('names a failed unload as an unload, with its slot', () => {
    const chip = holdChip(
      status({
        toolhead: {
          feed: 'empty',
          active_tray: null,
          was_feeding_tray: null,
          refill: { phase: 'failed', slot: 'AMS A slot 1', answer: 'no_movement', command: 'unload' },
        },
      }),
    );
    expect(keysOf(chip)).toEqual([
      'printers.toolhead.unloadFailed',
      'printers.toolhead.answer.no_movement',
      'printers.incidentAction.toolhead_refill',
    ]);
    expect(chip?.tooltip[0]?.values).toEqual({ slot: 'AMS A slot 1' });
  });

  it('names an unload that moved without finishing, and one with no slot', () => {
    const chip = holdChip(
      status({
        toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot: null, answer: 'acted', command: 'unload' } },
      }),
    );
    expect(keysOf(chip)).toEqual([
      'printers.toolhead.unloadFailedAnySlot',
      'printers.toolhead.answer.unloadActed',
      'printers.incidentAction.toolhead_refill',
    ]);
  });

  it('reads an explicit load the same as a missing command (an older backend)', () => {
    const explicit = holdChip(
      status({
        toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot: 'AMS A slot 1', answer: 'acted', command: 'load' } },
      }),
    );
    const missing = holdChip(
      status({ toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot: 'AMS A slot 1', answer: 'acted' } } }),
    );
    expect(keysOf(explicit)).toEqual([
      'printers.toolhead.loadFailed',
      'printers.toolhead.answer.loadActed',
      'printers.incidentAction.toolhead_refill',
    ]);
    expect(missing).toEqual(explicit);
  });

  it('keeps the slot qualifier of an incident row', () => {
    const chip = holdChip(status({ open_incident: incident('runout', 'recovering', 'AMS A slot 2'), toolhead: EMPTY }));
    expect(chip?.qualifier).toBe('AMS A slot 2');
  });

  it('emits only keys that resolve in en', () => {
    const answers = ['no_movement', 'acted'] as const;
    const commands = ['load', 'unload'] as const;
    const slots = ['AMS A slot 1', null] as const;
    const chips = [
      holdChip(status({ toolhead: LOADING })),
      holdChip(status({ toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'loading', slot: null, answer: null } } })),
      ...commands.flatMap((command) =>
        slots.flatMap((slot) =>
          answers.map((answer) =>
            holdChip(status({ toolhead: { feed: 'empty', active_tray: null, was_feeding_tray: null, refill: { phase: 'failed', slot, answer, command } } })),
          ),
        ),
      ),
      ...SPOKEN_REASONS.map((reason) => holdChip(status({ toolhead: emptyWith(reason) }))),
      holdChip(status({ open_incident: incident('toolhead_refill', 'recovering') })),
      holdChip(status({ open_incident: incident('toolhead_refill', 'escalated') })),
    ];
    for (const chip of chips) {
      for (const key of [chip!.label.key, ...chip!.tooltip.map((copy) => copy.key)]) {
        expect(typeof enValue(key), key).toBe('string');
      }
    }
  });
});
