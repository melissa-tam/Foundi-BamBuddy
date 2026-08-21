import type { InventorySpool, RespoolTrigger } from '../api/client';
import { remainingGrams } from './spoolGrams';

/**
 * The context `NewRollModal` runs on, and the ONE builder for the operator's own
 * slot verb (B4 slot-verb consolidation).
 *
 * It lives outside the component module because three surfaces build it — the
 * printer card, the SpoolBuddy slot picker, and the two prompt hooks — and the
 * form itself must never learn which of them opened it.
 */

/** Everything the form needs, normalised by the caller from whichever surface
 *  opened it. Deliberately NOT one of the WS prompt payloads: three different
 *  shapes reach this dialog and the modal must not know which. */
export interface NewRollContext {
  printer_id: number;
  ams_id: number;
  tray_id: number;
  /** The bound ledger row being retired — the endpoint's key. */
  spool_id: number;
  /** The bound row carries an RFID identity (either chip). Drives the required
   *  brand, the one-tag warning, the tag-id disclosure and the reused-tag copy;
   *  the backend re-derives it and owns the lane choice. */
  tagged: boolean;
  /** `manual` = the operator's own slot verb (nothing was asked); `prompt` = an
   *  answer to a raised question, which keeps that question's framing. */
  origin: 'manual' | 'prompt';
  /** Trays in the owning AMS, for the slot label. Defaults to 4. */
  tray_count?: number | null;
  /** Filament type, for the headline. */
  material?: string | null;
  /** Colour, as the AMS reports it, for the swatch. */
  rgba?: string | null;
  /** tag_uid or tray_uuid — rendered only inside the details disclosure. */
  tag_identity?: string | null;
  /** Grams the retiring row still claims (prompt framing). */
  remaining_g?: number | null;
  /** Grams already charged to the retiring row (manual framing). */
  used_g?: number | null;
  brand_prefill?: string | null;
  label_weight_prefill?: number | null;
  /** Why a re-spool prompt fired, so the evidence line can say the true thing. */
  trigger?: RespoolTrigger | null;
}

/**
 * Context for the operator's OWN slot verb — no question was raised, the click IS
 * the assertion. Shared by the printer card and the SpoolBuddy slot picker so the
 * two surfaces can never drift on tag-ness, grams or framing.
 *
 * Tag-ness is read from the row's visible identity columns. The frontend does not
 * carry the roll's SECOND chip (`sibling_tag_uid`), so this is a copy-and-validation
 * hint only: the backend re-derives tag-ness through `spool_tagless.is_tagless_spool`
 * and owns the lane choice, and a tagged row submitted without a brand comes back
 * 422 with the reason rendered inline.
 */
export function manualNewRollContext(
  printerId: number,
  amsId: number,
  trayId: number,
  spool: InventorySpool,
  trayCount?: number | null,
): NewRollContext {
  return {
    printer_id: printerId,
    ams_id: amsId,
    tray_id: trayId,
    spool_id: spool.id,
    tagged: !!(spool.tag_uid || spool.tray_uuid),
    origin: 'manual',
    tray_count: trayCount ?? null,
    material: spool.material,
    rgba: spool.rgba,
    tag_identity: spool.tag_uid || spool.tray_uuid,
    remaining_g: remainingGrams(spool),
    used_g: Math.max(0, Math.round(spool.weight_used ?? 0)),
  };
}
