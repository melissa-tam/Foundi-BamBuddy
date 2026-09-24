/**
 * PrinterMessageLines — the printer's RECORDED words on the printer card, one
 * line per message: under the hold chip ("Printer reported: …") and on the plate
 * row of a refused plate ("Plate check: …").
 *
 * Deliberately NOT part of `HMSErrorSummary` / `HMSErrorModal`: those render the
 * LIVE list and carry Clear/action verbs that act on the printer, which cannot
 * clear a recorded entry. Plain text, no verb, one muted line each — the chip or
 * the plate pill beside it is the line's emphasis. Renders nothing when there is
 * nothing to say.
 */
import { AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { PrinterMessage } from '../api/client';
import { InfoHint } from './ui/InfoHint';
import { distinctPrinterMessages, printerMessageText } from '../utils/printerMessages';

/** The two lines this renderer serves; each key's copy carries a `{{message}}` slot. */
export type PrinterMessageLabelKey = 'printers.holdMessage.reported' | 'printers.plateStatus.refusal';

interface PrinterMessageLinesProps {
  messages: readonly PrinterMessage[];
  labelKey: PrinterMessageLabelKey;
  /**
   * Supplementary detail for every line, carried by a focusable tooltip trigger
   * (react-best-practices §9 — never inline); omitted when the line needs none.
   */
  hint?: string;
  /** Placement in the caller's layout (spacing); the lines own no margin. */
  className?: string;
}

export function PrinterMessageLines({ messages, labelKey, hint, className }: PrinterMessageLinesProps) {
  const { t } = useTranslation();
  const lines = distinctPrinterMessages(messages);
  if (lines.length === 0) return null;

  return (
    <ul className={`space-y-0.5${className ? ` ${className}` : ''}`}>
      {lines.map((message) => (
        <li key={message.short_code} className="flex items-start gap-1.5 text-xs text-bambu-gray-light">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-status-warning" aria-hidden="true" />
          <span className="min-w-0 break-words">{t(labelKey, { message: printerMessageText(message) })}</span>
          {hint && <InfoHint text={hint} className="mt-0.5 flex-shrink-0" />}
        </li>
      ))}
    </ul>
  );
}
