import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import type { RespoolPromptMessage } from '../api/client';

/**
 * Queue of pending re-spool prompts, one per AMS slot.
 *
 * Mirrors `useUnknownTagPrompt` structurally: a per-slot queue fed by the
 * `respool-prompt` window CustomEvent (dispatched from the WebSocket handler
 * when the backend broadcasts the uncertain-tier `respool_prompt` event), with
 * de-duplication so a repeat event for the same slot doesn't stack a second
 * modal. The re-spool submission itself lives in `RespoolTagModal` — the single
 * form + mutation shared by this prompt path and the manual tray-menu path —
 * so this hook only owns the queue and the `dismiss` (dequeue) transition.
 */
function slotKey(printer_id: number, ams_id: number, tray_id: number): string {
  return `${printer_id}|${ams_id}|${tray_id}`;
}

export function useRespoolPrompt() {
  const { user, authEnabled } = useAuth();
  const [queue, setQueue] = useState<RespoolPromptMessage[]>([]);

  const isAuthed = !authEnabled || !!user;

  useEffect(() => {
    if (!isAuthed) return;
    const handler = (e: Event) => {
      const ce = e as CustomEvent<RespoolPromptMessage>;
      const detail = ce.detail;
      if (!detail || detail.printer_id == null) return;
      setQueue(prev => {
        // The backend dedupes per (slot, tag); a repeat event here means the
        // operator is still looking at the same modal, so don't re-queue it.
        const key = slotKey(detail.printer_id, detail.ams_id, detail.tray_id);
        if (prev.some(p => slotKey(p.printer_id, p.ams_id, p.tray_id) === key)) {
          return prev;
        }
        return [...prev, detail];
      });
    };
    window.addEventListener('respool-prompt', handler);
    return () => window.removeEventListener('respool-prompt', handler);
  }, [isAuthed]);

  const current = queue[0] ?? null;

  // Advance the queue — used both when the operator dismisses ("Same spool")
  // and after a successful re-spool submission closes the modal.
  const dismiss = useCallback(() => {
    setQueue(prev => prev.slice(1));
  }, []);

  return useMemo(
    () => ({
      prompt: current,
      dismiss,
    }),
    [current, dismiss],
  );
}
