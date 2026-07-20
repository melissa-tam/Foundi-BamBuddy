import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useToast, type ToastAction, type ToastType } from '../contexts/ToastContext';

/**
 * Shared per-AMS-slot prompt machinery — the ONE implementation of the mechanics
 * that both `useRespoolPrompt` (uncertain reused-tag) and `useTaglessFreshPrompt`
 * (fresh untagged roll) consume. Extracted so the two flows can't diverge.
 *
 * What it owns:
 *   - a per-slot queue keyed `${printer}|${ams}|${tray}` (dedup guard: a repeat
 *     event for a slot already queued never stacks a second toast),
 *   - a persistent warning-toast raised/refreshed per queued slot (idempotent by
 *     toast id — re-running never duplicates), cleared on answer,
 *   - cross-client dismissal via a WS→window-event bridge (another client
 *     answered → clear the matching slot's toast + queue entry here too),
 *   - reconnect replay: the raise effect re-runs whenever the queue changes, so a
 *     backend re-broadcast (bridged to `eventName`) re-raises cleanly.
 *
 * What the caller owns (per-flow divergence): the toast copy + actions
 * (`renderToast`), how an incoming window-event detail becomes a queued prompt
 * (`toPrompt`), and any extra state such as a review modal (`onSlotRemoved`
 * closes it when the reviewed slot is cleared elsewhere).
 */

export interface SlotTriple {
  printer_id: number;
  ams_id: number;
  tray_id: number;
}

/** Dedup key for one physical AMS slot. */
export function slotKey(printer_id: number, ams_id: number, tray_id: number): string {
  return `${printer_id}|${ams_id}|${tray_id}`;
}

function sameSlot(a: SlotTriple, b: SlotTriple): boolean {
  return slotKey(a.printer_id, a.ams_id, a.tray_id) === slotKey(b.printer_id, b.ams_id, b.tray_id);
}

/** The persistent-toast content a caller renders for one queued prompt. */
export interface SlotPromptToast {
  message: string;
  /** Defaults to `warning` (the shared prompt severity). */
  type?: ToastType;
  actions: ToastAction[];
}

/** Slot-clearing helpers handed to `renderToast` so action `onClick`s can clear
 *  the slot without the caller re-deriving the toast id. */
export interface SlotPromptHelpers {
  /** Full clear: dismiss the toast, dequeue, and run `onSlotRemoved`. */
  removeSlot: (triple: SlotTriple) => void;
  /** Take a slot out of the queue without touching its toast or `onSlotRemoved`
   *  (e.g. "Review…" hands the slot off to a modal). */
  dequeue: (triple: SlotTriple) => void;
  /** Dismiss just the slot's toast. */
  dismissSlotToast: (triple: SlotTriple) => void;
}

export interface UseSlotPromptOptions<T extends SlotTriple> {
  /** window event (bridged from a WS message) that enqueues a prompt. */
  eventName: string;
  /** window event (bridged from a WS broadcast) that clears a slot cross-client. */
  dismissedEventName?: string;
  /** toast-id prefix — `${prefix}-${printer}-${ams}-${tray}`. */
  toastIdPrefix: string;
  /** Only attach listeners when the user is authed (auth-disabled = always). */
  isAuthed: boolean;
  /** Validate/shape an incoming window-event detail into a queued prompt, or
   *  `null` to ignore it. Read through a ref, so it need not be memoized. */
  toPrompt: (detail: unknown) => T | null;
  /** Build the persistent toast for one queued prompt. MUST be memoized
   *  (`useCallback`) — it is a dependency of the raise effect, so a fresh
   *  identity every render would re-raise on every render. Receives slot-clearing
   *  helpers so its action handlers can clear the slot. */
  renderToast: (prompt: T, helpers: SlotPromptHelpers) => SlotPromptToast;
  /** Optional extra cleanup when a slot is fully removed (e.g. close a review
   *  modal if it was showing this slot). Read through a ref, so it need not be
   *  memoized. */
  onSlotRemoved?: (triple: SlotTriple) => void;
}

export interface UseSlotPromptResult<T extends SlotTriple> extends SlotPromptHelpers {
  /** The currently queued prompts (one per unanswered slot). */
  queue: T[];
}

export function useSlotPrompt<T extends SlotTriple>(
  options: UseSlotPromptOptions<T>,
): UseSlotPromptResult<T> {
  const { eventName, dismissedEventName, toastIdPrefix, isAuthed, renderToast } = options;
  const { showPersistentToast, dismissToast } = useToast();
  const [queue, setQueue] = useState<T[]>([]);

  // Callbacks that need not be memoized by the caller are read through refs so
  // the listener/raise effects stay stable (attach once, no needless re-runs).
  const toPromptRef = useRef(options.toPrompt);
  const onSlotRemovedRef = useRef(options.onSlotRemoved);
  useEffect(() => {
    toPromptRef.current = options.toPrompt;
    onSlotRemovedRef.current = options.onSlotRemoved;
  });

  const toastIdFor = useCallback(
    (triple: SlotTriple) =>
      `${toastIdPrefix}-${triple.printer_id}-${triple.ams_id}-${triple.tray_id}`,
    [toastIdPrefix],
  );

  const dequeue = useCallback((triple: SlotTriple) => {
    setQueue(prev => prev.filter(p => !sameSlot(p, triple)));
  }, []);

  const dismissSlotToast = useCallback(
    (triple: SlotTriple) => dismissToast(toastIdFor(triple)),
    [dismissToast, toastIdFor],
  );

  // Full clear: drop the toast, dequeue, and run the caller's extra cleanup.
  const removeSlot = useCallback(
    (triple: SlotTriple) => {
      dismissSlotToast(triple);
      dequeue(triple);
      onSlotRemovedRef.current?.(triple);
    },
    [dismissSlotToast, dequeue],
  );

  const helpers = useMemo<SlotPromptHelpers>(
    () => ({ removeSlot, dequeue, dismissSlotToast }),
    [removeSlot, dequeue, dismissSlotToast],
  );

  // Enqueue on a fresh event (dedup per slot). The backend dedupes per slot too,
  // so a repeat here is the same unanswered prompt — keep the first.
  useEffect(() => {
    if (!isAuthed) return;
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<unknown>).detail;
      const prompt = toPromptRef.current(detail);
      if (!prompt) return;
      setQueue(prev => (prev.some(p => sameSlot(p, prompt)) ? prev : [...prev, prompt]));
    };
    window.addEventListener(eventName, handler);
    return () => window.removeEventListener(eventName, handler);
  }, [isAuthed, eventName]);

  // Raise / refresh one persistent toast per queued slot. Idempotent by toast id,
  // so re-running (a new slot joins, or the copy changes on language switch)
  // never stacks duplicates.
  useEffect(() => {
    for (const prompt of queue) {
      const content = renderToast(prompt, helpers);
      showPersistentToast(toastIdFor(prompt), content.message, content.type ?? 'warning', {
        actions: content.actions,
      });
    }
  }, [queue, renderToast, helpers, showPersistentToast, toastIdFor]);

  // Cross-client dismissal: another client answered → clear the matching slot.
  useEffect(() => {
    if (!dismissedEventName) return;
    const handler = (e: Event) => {
      const d = (e as CustomEvent<Partial<SlotTriple>>).detail;
      if (!d || d.printer_id == null || d.ams_id == null || d.tray_id == null) return;
      removeSlot({ printer_id: d.printer_id, ams_id: d.ams_id, tray_id: d.tray_id });
    };
    window.addEventListener(dismissedEventName, handler);
    return () => window.removeEventListener(dismissedEventName, handler);
  }, [dismissedEventName, removeSlot]);

  return { queue, removeSlot, dequeue, dismissSlotToast };
}
