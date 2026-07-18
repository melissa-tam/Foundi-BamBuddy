import { useCallback, useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import type { Printer } from '../api/client';
import { api } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { useTranslation } from 'react-i18next';

/**
 * Quiet unknown-RFID-tag prompting, one entry per AMS slot.
 *
 * The AMS reports a tag with no inventory match; when the operator has
 * `auto_add_unknown_rfid` turned OFF the backend broadcasts an `unknown_tag`
 * event. This is a rare, non-urgent question — nothing is blocked by it — so it
 * no longer auto-opens a blocking modal. Instead each queued slot raises a
 * persistent, non-blocking toast (mirroring the respool prompt) with two
 * explicit answers:
 *   - "Add to Inventory" → mint a spool from the slot (Spoolman- or local-backed
 *                          per the `spoolman_enabled` setting) and clear the slot.
 *   - "Dismiss"          → clear the slot locally only. There is NO server-side
 *                          dismissal for unknown tags, so this is a hide-for-now.
 *
 * The queue is the per-slot state holder + dedup guard: a declarative effect
 * raises one toast per queued slot (the ToastContext dedups by id, so a repeat
 * event never stacks a second toast).
 */

export interface UnknownTagDetail {
  printer_id: number;
  ams_id: number;
  tray_id: number;
  tag_uid?: string;
  tray_uuid?: string;
  // Backend-provided so the prompt doesn't need to look up stale cached
  // `printerStatus` data — the React Query cache often lags the WS event by
  // several seconds while the new MQTT push is being applied.
  tray_type?: string | null;
  tray_color?: string | null;
  tray_sub_brands?: string | null;
  tray_count?: number | null;
}

export interface UnknownSpoolPrompt {
  printer_id: number;
  ams_id: number;
  tray_id: number;
  printer_name: string;
  /** Human-readable filament label ("Overture PETG" / "PLA"), used in the toast. */
  material: string;
}

interface SlotTriple {
  printer_id: number;
  ams_id: number;
  tray_id: number;
}

function slotKey(printer_id: number, ams_id: number, tray_id: number): string {
  return `${printer_id}|${ams_id}|${tray_id}`;
}

function toastId(printer_id: number, ams_id: number, tray_id: number): string {
  return `unknown-tag-${printer_id}-${ams_id}-${tray_id}`;
}

export function useUnknownTagPrompt() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showPersistentToast, dismissToast, showToast } = useToast();
  const { user, authEnabled } = useAuth();
  const [queue, setQueue] = useState<UnknownSpoolPrompt[]>([]);

  const isAuthed = !authEnabled || !!user;

  const buildPrompt = useCallback(
    (detail: UnknownTagDetail): UnknownSpoolPrompt | null => {
      // The backend only broadcasts unknown_tag for slots with real tray data,
      // and includes the relevant fields in the payload — no need to fall back
      // to the (often stale) cached printerStatus query for these.
      if (!detail.tray_type) return null;
      const printers = queryClient.getQueryData<Printer[]>(['printers']);
      const printer = printers?.find(p => p.id === detail.printer_id);
      const brand = detail.tray_sub_brands ?? null;
      const material = detail.tray_type ?? null;
      const label = brand ? `${brand} ${material ?? ''}`.trim() : material ?? '—';
      return {
        printer_id: detail.printer_id,
        ams_id: detail.ams_id,
        tray_id: detail.tray_id,
        printer_name: printer?.name ?? `Printer ${detail.printer_id}`,
        material: label,
      };
    },
    [queryClient],
  );

  const dequeue = useCallback((triple: SlotTriple) => {
    const key = slotKey(triple.printer_id, triple.ams_id, triple.tray_id);
    setQueue(prev => prev.filter(p => slotKey(p.printer_id, p.ams_id, p.tray_id) !== key));
  }, []);

  // Clear a slot: drop its toast and take it out of the queue (so the raise
  // effect can't resurrect the toast). Used by both actions.
  const removeSlot = useCallback(
    (triple: SlotTriple) => {
      dismissToast(toastId(triple.printer_id, triple.ams_id, triple.tray_id));
      dequeue(triple);
    },
    [dismissToast, dequeue],
  );

  const addMutation = useMutation({
    mutationFn: async (prompt: UnknownSpoolPrompt) => {
      const settings = queryClient.getQueryData<{ spoolman_enabled?: boolean }>(['settings']);
      if (settings?.spoolman_enabled) {
        await api.createSpoolmanSpoolFromSlot({
          printer_id: prompt.printer_id,
          ams_id: prompt.ams_id,
          tray_id: prompt.tray_id,
        });
      } else {
        await api.createSpoolFromSlot({
          printer_id: prompt.printer_id,
          ams_id: prompt.ams_id,
          tray_id: prompt.tray_id,
        });
      }
    },
    onSuccess: () => {
      showToast(t('inventory.addToInventorySuccess'), 'success');
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spool-assignments'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-slot-assignments'] });
      queryClient.invalidateQueries({ queryKey: ['linked-spools'] });
    },
    onError: (error: Error) => {
      showToast(error.message || t('inventory.addToInventoryFailed'), 'error');
    },
  });
  const { mutate: addSpool } = addMutation;

  // "Add to Inventory": clear the slot immediately (drop the toast + dequeue),
  // then mint the spool. onSuccess/onError surface their own toasts.
  const handleAdd = useCallback(
    (prompt: UnknownSpoolPrompt) => {
      removeSlot(prompt);
      addSpool(prompt);
    },
    [removeSlot, addSpool],
  );

  // "Dismiss": hide-for-now only. No server call — there is no server-side
  // dismissal for unknown tags.
  const handleDismiss = useCallback(
    (prompt: UnknownSpoolPrompt) => {
      removeSlot(prompt);
    },
    [removeSlot],
  );

  const raisePromptToast = useCallback(
    (prompt: UnknownSpoolPrompt) => {
      showPersistentToast(
        toastId(prompt.printer_id, prompt.ams_id, prompt.tray_id),
        t('inventory.unknownSpoolToast', {
          printer: prompt.printer_name,
          slot: prompt.tray_id + 1,
          material: prompt.material,
        }),
        'warning',
        {
          actions: [
            { label: t('inventory.addToInventory'), onClick: () => handleAdd(prompt) },
            { label: t('common.dismiss'), onClick: () => handleDismiss(prompt) },
          ],
        },
      );
    },
    [showPersistentToast, t, handleAdd, handleDismiss],
  );

  // Enqueue on a fresh `unknown-tag` event (dedup per slot).
  useEffect(() => {
    if (!isAuthed) return;
    const handler = (e: Event) => {
      const ce = e as CustomEvent<UnknownTagDetail>;
      const detail = ce.detail;
      if (!detail) return;
      const prompt = buildPrompt(detail);
      if (!prompt) return;
      setQueue(prev => {
        // Don't double-queue the same slot — the backend dedupes per
        // (slot, tag), so repeat events here are the same unanswered prompt.
        const key = slotKey(prompt.printer_id, prompt.ams_id, prompt.tray_id);
        if (prev.some(p => slotKey(p.printer_id, p.ams_id, p.tray_id) === key)) {
          return prev;
        }
        return [...prev, prompt];
      });
    };
    window.addEventListener('unknown-tag', handler);
    return () => window.removeEventListener('unknown-tag', handler);
  }, [isAuthed, buildPrompt]);

  // Raise / refresh one persistent toast per queued slot. Idempotent by toast
  // id, so re-running (e.g. a new slot joins) never stacks duplicates.
  useEffect(() => {
    for (const prompt of queue) {
      raisePromptToast(prompt);
    }
  }, [queue, raisePromptToast]);
}
