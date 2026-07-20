import { useCallback, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { api } from '../api/client';
import type { Printer, TaglessFreshPromptMessage } from '../api/client';
import {
  slotKey,
  useSlotPrompt,
  type SlotPromptHelpers,
  type SlotPromptToast,
  type SlotTriple,
} from './useSlotPrompt';

/**
 * Fresh-roll prompting for tagless (non-RFID) slots, one entry per AMS slot (W5).
 *
 * A tagless roll consumed past half its label weight (after a qualified physical
 * cycle) can't be told apart from a swapped-in fresh roll by RFID remain — there
 * is no tag — so the backend broadcasts `tagless_fresh_prompt` and each queued
 * slot raises a persistent, non-blocking toast with two answers:
 *   - "Same roll" → POST `tagless-fresh` answer:"same" (per-cycle dismiss — a
 *                   later roll swap re-asks) and clear the slot.
 *   - "Review…"   → open `TaglessFreshModal` to record the fresh roll.
 *
 * Shares all per-slot mechanics (queue + dedup, persistent toast raise/clear,
 * cross-client dismissal via the `tagless-fresh-prompt-dismissed` window-event
 * bridge) with `useRespoolPrompt` through `useSlotPrompt`; this hook layers on
 * only the tagless-specific copy/actions and the `activeContext` review modal.
 */
export function useTaglessFreshPrompt() {
  const { user, authEnabled } = useAuth();
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [activeContext, setActiveContext] = useState<TaglessFreshPromptMessage | null>(null);

  const isAuthed = !authEnabled || !!user;

  // A tagless-fresh prompt carries a printer id and the backing spool row id;
  // ignore malformed details.
  const toPrompt = useCallback((detail: unknown): TaglessFreshPromptMessage | null => {
    const d = detail as TaglessFreshPromptMessage | null;
    if (!d || d.printer_id == null || d.spool_id == null) return null;
    return d;
  }, []);

  // "Same roll": record a per-cycle dismissal (the next qualified physical cycle
  // re-asks), then clear the slot.
  const handleSameRoll = useCallback(
    (prompt: TaglessFreshPromptMessage, removeSlot: (triple: SlotTriple) => void) => {
      const triple: SlotTriple = {
        printer_id: prompt.printer_id,
        ams_id: prompt.ams_id,
        tray_id: prompt.tray_id,
      };
      api
        .taglessFresh(prompt.spool_id, { ...triple, answer: 'same' })
        .then(() => removeSlot(triple))
        .catch((error: Error) =>
          showToast(error.message || t('inventory.freshRoll.dismissFailed'), 'error'),
        );
    },
    [showToast, t],
  );

  // "Review…": hide the toast, take the slot out of the queue (so the raise
  // effect can't resurrect it while the modal is open), and open the modal.
  const handleReview = useCallback(
    (prompt: TaglessFreshPromptMessage, helpers: SlotPromptHelpers) => {
      helpers.dismissSlotToast(prompt);
      helpers.dequeue(prompt);
      setActiveContext(prompt);
    },
    [],
  );

  const renderToast = useCallback(
    (prompt: TaglessFreshPromptMessage, helpers: SlotPromptHelpers): SlotPromptToast => {
      const printers = queryClient.getQueryData<Printer[]>(['printers']);
      const printerName =
        printers?.find(p => p.id === prompt.printer_id)?.name ?? `Printer ${prompt.printer_id}`;
      return {
        message: t('inventory.freshRoll.promptToast', {
          printer: printerName,
          slot: prompt.tray_id + 1,
        }),
        type: 'warning',
        actions: [
          {
            label: t('inventory.freshRoll.sameRoll'),
            onClick: () => handleSameRoll(prompt, helpers.removeSlot),
          },
          { label: t('inventory.freshRoll.reviewAction'), onClick: () => handleReview(prompt, helpers) },
        ],
      };
    },
    [queryClient, t, handleSameRoll, handleReview],
  );

  // Cross-client answer cleared this slot — close the modal if it was showing it.
  const handleSlotRemoved = useCallback((triple: SlotTriple) => {
    setActiveContext(prev =>
      prev && slotKey(prev.printer_id, prev.ams_id, prev.tray_id) ===
        slotKey(triple.printer_id, triple.ams_id, triple.tray_id)
        ? null
        : prev,
    );
  }, []);

  useSlotPrompt<TaglessFreshPromptMessage>({
    eventName: 'tagless-fresh-prompt',
    dismissedEventName: 'tagless-fresh-prompt-dismissed',
    toastIdPrefix: 'tagless-fresh',
    isAuthed,
    toPrompt,
    renderToast,
    onSlotRemoved: handleSlotRemoved,
  });

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
