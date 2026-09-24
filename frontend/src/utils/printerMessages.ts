/**
 * The printer's RECORDED words (`PrinterMessage`) — what a hold or a refused
 * plate keeps of the HMS dialog the printer showed, after a ladder verb, a stop
 * or the next job cleared it off the printer.
 *
 * Pure derivations only; `components/PrinterMessageLines` is the one renderer.
 * The live list and the recorded list are compared on `short_code` — the one key
 * both carry (a recorded message has no `full_code`).
 */
import type { HMSError, PrinterMessage } from '../api/client';
import { formatHmsCode } from './hmsCode';

/**
 * The line text for one recorded message: the vendor description, or the code
 * itself when the catalog does not know it — the same fallback the live HMS
 * summary applies, so an unknown code never renders as an empty line.
 */
export function printerMessageText(message: PrinterMessage): string {
  return message.description || formatHmsCode(undefined, message.short_code);
}

/**
 * One message per short code, first recorded wins. Two full codes can share a
 * short code (the middle groups differ), and would otherwise read as the same
 * sentence twice.
 */
export function distinctPrinterMessages(messages: readonly PrinterMessage[]): PrinterMessage[] {
  const seen = new Set<string>();
  return messages.filter((message) => {
    if (seen.has(message.short_code)) return false;
    seen.add(message.short_code);
    return true;
  });
}

/**
 * The recorded messages the printer no longer shows. A code still on the live
 * list is already on the card (the HMS summary and modal), so a second line for
 * it would be a duplicate surface — and the live surfaces own the Clear/action
 * verbs, which act on the printer and cannot clear a recorded entry.
 */
export function unshownPrinterMessages(
  recorded: readonly PrinterMessage[],
  live: readonly HMSError[],
): PrinterMessage[] {
  const shownLive = new Set(live.map((error) => error.short_code));
  return recorded.filter((message) => !shownLive.has(message.short_code));
}
