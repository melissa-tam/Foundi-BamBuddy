import { useCallback, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { api } from '../api/client';
import type { Printer, RespoolPromptMessage } from '../api/client';
import type { NewRollContext } from '../utils/newRollContext';
import { formatDuration } from '../utils/date';
import {
  slotKey,
  useSlotPrompt,
  type SlotPromptHelpers,
  type SlotPromptToast,
  type SlotTriple,
} from './useSlotPrompt';

/**
 * Quiet, ask-once re-spool prompting, one entry per AMS slot.
 *
 * The uncertain-tier `respool_prompt` no longer auto-opens the modal (a blocking
 * dialog for a maybe-spent spool was too noisy). Instead each queued slot raises
 * a persistent, non-blocking toast — worded from the prompt's `trigger`, so an
 * "almost empty" spool is never announced as a detected reused tag — with two
 * explicit answers:
 *   - "Same spool"  → POST `respool-dismiss` (persists the answer so the prompt
 *                     never fires again for this spool) and clear the slot.
 *   - "Review…"     → open the shared `NewRollModal` on this slot, which posts the
 *                     merged `POST /inventory/spools/{id}/new-roll`. Offered only
 *                     when the prompt names a donor row — with no row there is
 *                     nothing to retire and the form could not be keyed.
 * Dismissing the toast (X) is deliberately NOT an answer — it just hides the
 * toast for now.
 *
 * The prompt is an ESCALATION, not a question the farm could have answered
 * itself: it knows what happened to the slot and cannot carry it out without the
 * operator naming the replacement roll.
 *
 * All the per-slot mechanics — queue + dedup, persistent toast raise/clear,
 * cross-client dismissal via the `respool-prompt-dismissed` window-event bridge
 * (from the WS `respool_prompt_dismissed` broadcast and from `spool_respooled`) —
 * live in the shared `useSlotPrompt` helper. This hook layers on only the
 * respool-specific copy/actions and the single `activeContext` modal slot opened
 * by "Review…".
 */
export function useRespoolPrompt() {
  const { user, authEnabled } = useAuth();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [activeContext, setActiveContext] = useState<NewRollContext | null>(null);

  const isAuthed = !authEnabled || !!user;

  // Only respool-prompt events carry a printer id; ignore malformed details.
  const toPrompt = useCallback((detail: unknown): RespoolPromptMessage | null => {
    const d = detail as RespoolPromptMessage | null;
    if (!d || d.printer_id == null) return null;
    return d;
  }, []);

  // "Same spool": persist the dismissal so the prompt never re-fires for this
  // spool, then clear the slot. A prompt with no backing donor row (nothing to
  // stamp) is cleared locally only.
  const handleSameSpool = useCallback(
    (prompt: RespoolPromptMessage, removeSlot: (triple: SlotTriple) => void) => {
      const triple: SlotTriple = {
        printer_id: prompt.printer_id,
        ams_id: prompt.ams_id,
        tray_id: prompt.tray_id,
      };
      if (prompt.donor_spool_id == null) {
        removeSlot(triple);
        return;
      }
      api
        .dismissRespoolPrompt(prompt.donor_spool_id, triple)
        .then(() => removeSlot(triple))
        .catch((error: Error) =>
          showToast(error.message || t('inventory.respool.dismissFailed'), 'error'),
        );
    },
    [showToast, t],
  );

  // "Review…": hide the toast, take the slot out of the queue (so the raise
  // effect can't resurrect the toast while the modal is open), and open the
  // shared new-roll form on this slot.
  //
  // The WS payload is translated into the form's own context here rather than
  // being handed over raw: three surfaces open that one form, and it must not
  // know which of them did. `donor_spool_id` is the row the operator is retiring,
  // and therefore the endpoint's key — the caller guarantees it is non-null by
  // only offering "Review…" when the prompt names one.
  const handleReview = useCallback(
    (prompt: RespoolPromptMessage, helpers: SlotPromptHelpers) => {
      if (prompt.donor_spool_id == null) return;
      helpers.dismissSlotToast(prompt);
      helpers.dequeue(prompt);
      setActiveContext({
        printer_id: prompt.printer_id,
        ams_id: prompt.ams_id,
        tray_id: prompt.tray_id,
        spool_id: prompt.donor_spool_id,
        tagged: true,
        origin: 'prompt',
        tray_count: prompt.tray_count,
        material: prompt.tray_sub_brands || prompt.tray_type,
        rgba: prompt.tray_color,
        tag_identity: prompt.tag_uid || prompt.tray_uuid,
        // An impossible ledger (weight_used past the label) makes every derived
        // "remaining" meaningless — prod prompts announced "remaining −792.9 g".
        // The question stands; only the number is withdrawn, exactly as the toast
        // does it.
        remaining_g: prompt.ledger_unreliable ? null : prompt.donor_remaining_g,
        brand_prefill: prompt.brand_prefill,
        label_weight_prefill: prompt.label_weight_prefill,
        trigger: prompt.trigger ?? null,
      });
    },
    [],
  );

  const renderToast = useCallback(
    (prompt: RespoolPromptMessage, helpers: SlotPromptHelpers): SlotPromptToast => {
      const printers = queryClient.getQueryData<Printer[]>(['printers']);
      const printerName =
        printers?.find(p => p.id === prompt.printer_id)?.name ?? `Printer ${prompt.printer_id}`;
      const base = { printer: printerName, slot: prompt.tray_id + 1 };

      // Provenance clause shared by the spent and remain_jump copies: only when
      // BOTH the live AMS % and the ledger % are known does it say the numbers the
      // operator needs to judge a stale question ("AMS reports ~X%; records say Y%").
      // An IMPOSSIBLE donor ledger (weight_used past the label) makes every derived
      // "remaining" meaningless — prod prompts announced "remaining −792.9 g". The
      // question still stands (a spent+loaded spool deserves it), so the prompt is not
      // suppressed; only the numbers are, replaced by a clause that says the record is
      // untrustworthy. That is the honest thing to show and it points at the real fix.
      const numbersClause = (): string | null => {
        if (prompt.ledger_unreliable) return t('inventory.respool.ledgerUnreliable');
        const ams = prompt.ams_remain_pct;
        const ledger = prompt.ledger_remain_pct;
        if (ams == null || ledger == null) return null;
        return t('inventory.respool.spentToastNumbers', { ams, ledger: Math.round(ledger) });
      };
      const appended = (message: string, clause: string | null): string =>
        clause ? `${message} ${clause}` : message;

      // Say the true thing, with provenance:
      //  - `spent` with a known age → state the evidence (a runout signal) and how
      //    long ago it fired, so a days-old false stamp reads as stale, not fresh.
      //  - `near_empty` → the record is nearly used up and somebody handled the
      //    slot; it is NOT a reused tag (announcing one was how two false popups
      //    reached the operator, 2026-07-20). Append the grams still on the ledger.
      //  - `remain_jump` (and the manual tray-menu path, which carries no trigger,
      //    and a `spent` prompt with no age) keep the reused-tag framing.
      let message: string;
      if (prompt.trigger === 'spent' && prompt.spent_age_s != null && Number.isFinite(prompt.spent_age_s)) {
        message = appended(
          t('inventory.respool.spentToast', { ...base, age: formatDuration(prompt.spent_age_s) }),
          numbersClause(),
        );
      } else if (prompt.trigger === 'near_empty') {
        message = t('inventory.respool.nearEmptyToast', base);
        const grams = prompt.donor_remaining_g;
        if (grams != null && Number.isFinite(grams) && grams >= 0) {
          message = `${message} ${t('inventory.respool.nearEmptyToastRemaining', { remaining: Math.round(grams) })}`;
        }
      } else {
        message = t('inventory.respool.reusedTagToast', base);
        if (prompt.trigger === 'remain_jump') {
          message = appended(message, numbersClause());
        }
      }

      return {
        message,
        type: 'warning',
        actions: [
          {
            label: t('inventory.respool.sameSpoolAction'),
            onClick: () => handleSameSpool(prompt, helpers.removeSlot),
          },
          // No donor row → nothing to retire, so the form has no key and the offer
          // would dead-end. "Same spool" still answers the question.
          ...(prompt.donor_spool_id != null
            ? [
                {
                  label: t('inventory.respool.reviewAction'),
                  onClick: () => handleReview(prompt, helpers),
                },
              ]
            : []),
        ],
      };
    },
    [queryClient, t, handleSameSpool, handleReview],
  );

  // Cross-client / auto re-spool cleared this slot — close the modal if it was
  // the one being reviewed.
  const handleSlotRemoved = useCallback((triple: SlotTriple) => {
    setActiveContext(prev =>
      prev && slotKey(prev.printer_id, prev.ams_id, prev.tray_id) ===
        slotKey(triple.printer_id, triple.ams_id, triple.tray_id)
        ? null
        : prev,
    );
  }, []);

  useSlotPrompt<RespoolPromptMessage>({
    eventName: 'respool-prompt',
    dismissedEventName: 'respool-prompt-dismissed',
    toastIdPrefix: 'respool',
    isAuthed,
    toPrompt,
    renderToast,
    onSlotRemoved: handleSlotRemoved,
  });

  // Modal onClose — the reviewed slot was already dequeued + toast-dismissed at
  // "Review…", so closing (successful re-spool or cancel) just drops the modal.
  const closeModal = useCallback(() => {
    setActiveContext(null);
  }, []);

  return useMemo(
    () => ({
      activeContext,
      closeModal,
    }),
    [activeContext, closeModal],
  );
}
