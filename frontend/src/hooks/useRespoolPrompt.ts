import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { api } from '../api/client';
import type { Printer, RespoolPromptMessage } from '../api/client';

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
 * The queue stays the per-slot state holder + dedup guard: a declarative effect
 * raises/refreshes one toast per queued slot (the ToastContext dedups by id, so
 * a repeat event never stacks a second toast). `activeContext` is the single
 * slot currently open in the modal (set only by "Review…"). Cross-client sync: a
 * `respool-prompt-dismissed` window event (bridged from the WS
 * `respool_prompt_dismissed` broadcast, and from `spool_respooled`) clears the
 * matching slot's toast + queue entry + open modal on every client.
 */
function slotKey(printer_id: number, ams_id: number, tray_id: number): string {
  return `${printer_id}|${ams_id}|${tray_id}`;
}

function toastId(printer_id: number, ams_id: number, tray_id: number): string {
  return `respool-${printer_id}-${ams_id}-${tray_id}`;
}

interface SlotTriple {
  printer_id: number;
  ams_id: number;
  tray_id: number;
}

export function useRespoolPrompt() {
  const { user, authEnabled } = useAuth();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showPersistentToast, dismissToast, showToast } = useToast();

  const [queue, setQueue] = useState<RespoolPromptMessage[]>([]);
  const [activeContext, setActiveContext] = useState<RespoolPromptMessage | null>(null);

  const isAuthed = !authEnabled || !!user;

  const dequeue = useCallback((triple: SlotTriple) => {
    const key = slotKey(triple.printer_id, triple.ams_id, triple.tray_id);
    setQueue(prev => prev.filter(p => slotKey(p.printer_id, p.ams_id, p.tray_id) !== key));
  }, []);

  // Clear a slot everywhere: dequeue, drop its toast, and close the modal if it
  // was the one being reviewed. Used by "Same spool" and cross-client dismissal.
  const removeSlot = useCallback(
    (triple: SlotTriple) => {
      const key = slotKey(triple.printer_id, triple.ams_id, triple.tray_id);
      dismissToast(toastId(triple.printer_id, triple.ams_id, triple.tray_id));
      dequeue(triple);
      setActiveContext(prev =>
        prev && slotKey(prev.printer_id, prev.ams_id, prev.tray_id) === key ? null : prev,
      );
    },
    [dismissToast, dequeue],
  );

  // "Same spool": persist the dismissal so the prompt never re-fires for this
  // spool, then clear the slot. A prompt with no backing donor row (nothing to
  // stamp) is cleared locally only.
  const handleSameSpool = useCallback(
    (prompt: RespoolPromptMessage) => {
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
    [removeSlot, showToast, t],
  );

  // "Review…": hide the toast, take the slot out of the queue (so the raise
  // effect can't resurrect the toast while the modal is open), and open the
  // modal on this slot.
  const handleReview = useCallback(
    (prompt: RespoolPromptMessage) => {
      dismissToast(toastId(prompt.printer_id, prompt.ams_id, prompt.tray_id));
      dequeue(prompt);
      setActiveContext(prompt);
    },
    [dismissToast, dequeue],
  );

  const raisePromptToast = useCallback(
    (prompt: RespoolPromptMessage) => {
      const printers = queryClient.getQueryData<Printer[]>(['printers']);
      const printerName =
        printers?.find(p => p.id === prompt.printer_id)?.name ?? `Printer ${prompt.printer_id}`;
      showPersistentToast(
        toastId(prompt.printer_id, prompt.ams_id, prompt.tray_id),
        t('inventory.respool.promptToast', { printer: printerName, slot: prompt.tray_id + 1 }),
        'warning',
        {
          actions: [
            { label: t('inventory.respool.sameSpoolAction'), onClick: () => handleSameSpool(prompt) },
            { label: t('inventory.respool.reviewAction'), onClick: () => handleReview(prompt) },
          ],
        },
      );
    },
    [queryClient, showPersistentToast, t, handleSameSpool, handleReview],
  );

  // Enqueue on a fresh `respool-prompt` event (dedup per slot).
  useEffect(() => {
    if (!isAuthed) return;
    const handler = (e: Event) => {
      const ce = e as CustomEvent<RespoolPromptMessage>;
      const detail = ce.detail;
      if (!detail || detail.printer_id == null) return;
      const key = slotKey(detail.printer_id, detail.ams_id, detail.tray_id);
      setQueue(prev =>
        prev.some(p => slotKey(p.printer_id, p.ams_id, p.tray_id) === key) ? prev : [...prev, detail],
      );
    };
    window.addEventListener('respool-prompt', handler);
    return () => window.removeEventListener('respool-prompt', handler);
  }, [isAuthed]);

  // Raise / refresh one persistent toast per queued slot. Idempotent by toast id,
  // so re-running (e.g. a new slot joins) never stacks duplicates.
  useEffect(() => {
    for (const prompt of queue) {
      raisePromptToast(prompt);
    }
  }, [queue, raisePromptToast]);

  // Cross-client dismissal: another client answered "Same spool" (WS
  // `respool_prompt_dismissed`) or an auto/manual re-spool ran (`spool_respooled`).
  // Clear the matching slot here too.
  useEffect(() => {
    const handler = (e: Event) => {
      const ce = e as CustomEvent<Partial<SlotTriple>>;
      const d = ce.detail;
      if (!d || d.printer_id == null || d.ams_id == null || d.tray_id == null) return;
      removeSlot({ printer_id: d.printer_id, ams_id: d.ams_id, tray_id: d.tray_id });
    };
    window.addEventListener('respool-prompt-dismissed', handler);
    return () => window.removeEventListener('respool-prompt-dismissed', handler);
  }, [removeSlot]);

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
