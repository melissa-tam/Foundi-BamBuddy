import { useCallback, useEffect, useRef } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { api } from '../api/client';
import type { RespoolPromptMessage, TaglessFreshPromptMessage } from '../api/client';

/**
 * Recovery lane for the per-slot operator prompts (fresh-roll + re-spool).
 *
 * The websocket broadcast is the ONLY delivery channel for those questions, and
 * it is fire-and-forget: a client that was not connected when it fired never
 * learned about it, with no surface to recover from (prod 2026-07-24 — a tab
 * stuck in a ws-token 401 loop meant a "Fresh roll?" prompt reached zero
 * clients and stayed invisible until somebody noticed the spool).
 *
 * So on mount AND on every websocket (re)connect we ask the backend which
 * prompts are still live and replay each one through the SAME window events the
 * WS handler bridges to (`tagless-fresh-prompt` / `respool-prompt`) with the
 * SAME detail shape (see `useWebSocket.ts`, cases `tagless_fresh_prompt` and
 * `respool_prompt`). The prompt hooks therefore need no knowledge of this lane:
 * a replayed event is indistinguishable from a live broadcast, and
 * `useSlotPrompt` re-raises an already-queued slot whose toast was dismissed.
 *
 * Side-effect only — mounted once in `Layout`. It must NEVER be able to break
 * the shell it hangs off, so every fetch failure is swallowed (this lane exists
 * to add resilience; the live WS path is unaffected when it fails).
 */
export function usePendingPromptSync(): void {
  const { user, authEnabled } = useAuth();
  const isAuthed = !authEnabled || !!user;

  // In-flight guard: a reconnect storm (or mount racing the first `ws-connected`)
  // must fire ONE fetch, not one per event. Dropping the concurrent triggers is
  // safe — they would return the same answer, and the next reconnect re-asks.
  const inFlightRef = useRef(false);

  const syncPendingPrompts = useCallback(async (): Promise<void> => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const pending = await api.getPendingPrompts();
      for (const m of pending.fresh ?? []) {
        window.dispatchEvent(
          new CustomEvent<TaglessFreshPromptMessage>('tagless-fresh-prompt', {
            detail: {
              printer_id: m.printer_id,
              ams_id: m.ams_id,
              tray_id: m.tray_id,
              spool_id: m.spool_id,
              remaining_g: m.remaining_g,
              material: m.material,
              rgba: m.rgba,
            },
          }),
        );
      }
      for (const m of pending.respool ?? []) {
        window.dispatchEvent(
          new CustomEvent<RespoolPromptMessage>('respool-prompt', {
            detail: {
              printer_id: m.printer_id,
              ams_id: m.ams_id,
              tray_id: m.tray_id,
              tag_uid: m.tag_uid,
              tray_uuid: m.tray_uuid,
              tray_type: m.tray_type,
              tray_color: m.tray_color,
              tray_sub_brands: m.tray_sub_brands,
              tray_count: m.tray_count,
              donor_spool_id: m.donor_spool_id,
              donor_remaining_g: m.donor_remaining_g,
              brand_prefill: m.brand_prefill,
              label_weight_prefill: m.label_weight_prefill,
              // Same field set the live WS bridge forwards — the REST replay must
              // produce a byte-identical prompt or a reconnect would silently change
              // the copy the operator sees.
              trigger: m.trigger,
              spent_at: m.spent_at,
              spent_age_s: m.spent_age_s,
              ams_remain_pct: m.ams_remain_pct,
              ledger_remain_pct: m.ledger_remain_pct,
              bound_since: m.bound_since,
              ledger_unreliable: m.ledger_unreliable,
            },
          }),
        );
      }
    } catch (error) {
      // Recovery lane, not a feature: a failed poll leaves the live WS path
      // exactly as it was. Never toast, never throw.
      console.warn('[PendingPromptSync] Could not fetch pending prompts', error);
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    if (!isAuthed) return;
    // Page load / login: catch up on anything broadcast while this tab was away.
    void syncPendingPrompts();
    // Reconnect: `useWebSocket`'s `onopen` fires this — the gap it just closed is
    // precisely the window whose broadcasts we missed.
    const handler = () => void syncPendingPrompts();
    window.addEventListener('ws-connected', handler);
    return () => window.removeEventListener('ws-connected', handler);
  }, [isAuthed, syncPendingPrompts]);
}
