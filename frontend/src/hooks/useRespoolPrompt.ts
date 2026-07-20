import { useCallback, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { api } from '../api/client';
import type { Printer, RespoolPromptMessage } from '../api/client';
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
 * a persistent, non-blocking toast with two explicit answers:
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
      return {
        message: t('inventory.respool.promptToast', { printer: printerName, slot: prompt.tray_id + 1 }),
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
