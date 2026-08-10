/**
 * The ONE derivation of "a spool binding sits on a slot with no readable
 * filament in it — what should the operator be told?" (W5a/W5b; presence
 * context added 2026-08-10).
 *
 * Two surfaces render this state and they must agree on both the precedence
 * and the wording: the printer card's `EmptySlotHoverCard` (slot-side) and the
 * Inventory LOCATION column (spool-side). Keeping the mapping here means a
 * fifth state — or a reworded fourth — lands in both places at once.
 *
 * It is now genuinely the one derivation: callers used to decide "not inserted"
 * from `{spent, preConfigured}` alone, with no idea whether anything was in the
 * slot, so a seated-but-unread roll rendered the flat contradiction the operator
 * photographed — "Spool present — unrecognized" in the header above "Still
 * assigned … not inserted". Presence is an INPUT here now, and "not inserted" is
 * stated only when presence is actually false.
 *
 * Precedence is deliberate: a runout latch (`spool.spent_at`) OUTRANKS a
 * deliberate bind-to-empty (`assignment.pre_configured_at`), and both outrank
 * the presence-derived tail. `pre_configured_at` is a bind-time stamp that a
 * later insert-then-run-dry cycle never clears, so once the roll is spent
 * "awaiting insert" is stale history while "ran out" is the actionable truth.
 */
import { AlertTriangle, Clock, HelpCircle, MinusCircle, Search, type LucideIcon } from 'lucide-react';
import type { EmptySlotKind } from './amsHelpers';

export type SpoolBindingStatusId =
  | 'ranOut'
  | 'awaitingInsert'
  | 'seatedUnread'
  | 'notInserted'
  | 'presenceUnknown';

export interface SpoolBindingStatus {
  id: SpoolBindingStatusId;
  Icon: LucideIcon;
  /** Tailwind colour for the ICON. Never the sole carrier of meaning — every
   *  call site renders the `i18nKey` sentence beside it (WCAG 1.4.1). */
  className: string;
  i18nKey: `ams.emptySlotBinding.${SpoolBindingStatusId}`;
}

/**
 * Live presence of the slot a binding claims — the client-side name for the
 * backend tri-state (`tray_fields.tray_presence` / `SpoolAssignment.present`).
 * `unknown` is a first-class answer, never collapsed into `empty`.
 */
export type SlotPresence = 'seated' | 'empty' | 'unknown';

/**
 * Resolve slot presence from whichever evidence a caller holds.
 *
 * `kind` is the live wire classification of the tray being rendered
 * (`getEmptySlotKind`) — preferred when present because it is derived from the
 * very status push this render is showing. `present` is the API's per-request
 * tri-state (`SpoolAssignment.present`), which is what the spool-side surfaces
 * have instead of a tray, and which can still ANSWER when the tray-level wire
 * facts do not (it also sees the unit-level exist bits). So an `unknown` kind
 * defers to it rather than overriding it: evidence is only ever added here.
 */
export function slotPresence(input: {
  kind?: EmptySlotKind | null;
  present?: boolean | null;
}): SlotPresence {
  if (input.kind === 'present') return 'seated';
  if (input.kind === 'physical') return 'empty';
  if (input.present === true) return 'seated';
  if (input.present === false) return 'empty';
  return 'unknown';
}

/**
 * Map the flags a caller can observe onto the operator-facing state.
 *
 * Callers decide WHEN to ask (the hover card asks for any binding on a slot with
 * no readable filament; the inventory column asks whenever the assignment is not
 * positively present) — this function only answers WHAT it means. `presence`
 * defaults to `unknown`, which is the honest answer for a caller that has not
 * looked: it says the slot is unconfirmed instead of asserting an empty one.
 */
export function resolveSpoolBindingStatus(binding: {
  /** `spool.spent_at` is set — the roll ran dry. */
  spent: boolean;
  /** `assignment.pre_configured_at` is set — bound before the physical insert. */
  preConfigured: boolean;
  /** Live presence of the claimed slot (see {@link slotPresence}). */
  presence?: SlotPresence;
}): SpoolBindingStatus {
  if (binding.spent) {
    return {
      id: 'ranOut',
      Icon: AlertTriangle,
      className: 'text-amber-400',
      i18nKey: 'ams.emptySlotBinding.ranOut',
    };
  }
  if (binding.preConfigured) {
    return {
      id: 'awaitingInsert',
      Icon: Clock,
      className: 'text-bambu-blue',
      i18nKey: 'ams.emptySlotBinding.awaitingInsert',
    };
  }
  if (binding.presence === 'seated') {
    // Something IS in the slot; the AMS just has not named it yet. The binding is
    // very probably describing exactly that roll, so this is a wait, not a fault.
    return {
      id: 'seatedUnread',
      Icon: Search,
      className: 'text-amber-400',
      i18nKey: 'ams.emptySlotBinding.seatedUnread',
    };
  }
  if (binding.presence === 'empty') {
    return {
      id: 'notInserted',
      Icon: MinusCircle,
      className: 'text-bambu-gray',
      i18nKey: 'ams.emptySlotBinding.notInserted',
    };
  }
  return {
    id: 'presenceUnknown',
    Icon: HelpCircle,
    className: 'text-bambu-gray',
    i18nKey: 'ams.emptySlotBinding.presenceUnknown',
  };
}
