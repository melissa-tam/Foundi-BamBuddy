import { useCallback, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { api } from '../api/client';
import type { Printer, RespoolPromptMessage } from '../api/client';
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
 *   - "Review…"     → open `RespoolTagModal` for the full re-spool form.
 * Dismissing the toast (X) is deliberately NOT an answer — it just hides the
 * toast for now.
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

  const [activeContext, setActiveContext] = useState<RespoolPromptMessage | null>(null);

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
  // modal on this slot.
  const handleReview = useCallback(
    (prompt: RespoolPromptMessage, helpers: SlotPromptHelpers) => {
      helpers.dismissSlotToast(prompt);
      helpers.dequeue(prompt);
      setActiveContext(prompt);
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
      const numbersClause = (): string | null => {
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
          { label: t('inventory.respool.reviewAction'), onClick: () => handleReview(prompt, helpers) },
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
