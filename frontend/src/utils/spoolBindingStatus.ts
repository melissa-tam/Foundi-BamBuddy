/**
 * The ONE derivation of "a spool binding is still on a slot that has no
 * filament in it — why?" (W5a/W5b).
 *
 * Two surfaces render this state and they must agree on both the precedence
 * and the wording: the printer card's `EmptySlotHoverCard` (slot-side) and the
 * Inventory LOCATION column (spool-side). Keeping the mapping here means a
 * fourth state — or a reworded third — lands in both places at once.
 *
 * Precedence is deliberate: a runout latch (`spool.spent_at`) OUTRANKS a
 * deliberate bind-to-empty (`assignment.pre_configured_at`). `pre_configured_at`
 * is a bind-time stamp that a later insert-then-run-dry cycle never clears, so
 * once the roll is spent "awaiting insert" is stale history while "ran out" is
 * the actionable truth.
 */
import { AlertTriangle, Clock, MinusCircle, type LucideIcon } from 'lucide-react';

export type SpoolBindingStatusId = 'ranOut' | 'awaitingInsert' | 'notInserted';

export interface SpoolBindingStatus {
  id: SpoolBindingStatusId;
  Icon: LucideIcon;
  /** Tailwind colour for the ICON. Never the sole carrier of meaning — every
   *  call site renders the `i18nKey` sentence beside it (WCAG 1.4.1). */
  className: string;
  i18nKey: `ams.emptySlotBinding.${SpoolBindingStatusId}`;
}

/**
 * Map the two flags a caller can observe onto the operator-facing state.
 *
 * Callers decide WHEN to ask (the hover card asks for any binding on an empty
 * slot; the inventory column asks only when the assignment reports
 * `present === false`) — this function only answers WHAT it means.
 */
export function resolveSpoolBindingStatus(binding: {
  /** `spool.spent_at` is set — the roll ran dry. */
  spent: boolean;
  /** `assignment.pre_configured_at` is set — bound before the physical insert. */
  preConfigured: boolean;
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
  return {
    id: 'notInserted',
    Icon: MinusCircle,
    className: 'text-bambu-gray',
    i18nKey: 'ams.emptySlotBinding.notInserted',
  };
}
