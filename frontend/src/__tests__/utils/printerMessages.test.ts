/**
 * The printer's RECORDED words on a hold (`utils/printerMessages`): which ones
 * the card shows, and what text a line carries. The live HMS list and the
 * recorded list meet on `short_code` — the one key both carry.
 */
import { describe, it, expect } from 'vitest';
import type { HMSError, PrinterMessage } from '../../api/client';
import {
  distinctPrinterMessages,
  printerMessageText,
  unshownPrinterMessages,
} from '../../utils/printerMessages';

const PLATE_CHECK: PrinterMessage = {
  short_code: '0500_808C',
  description: 'Detected build plate offset or debris',
};
const FEED: PrinterMessage = { short_code: '0700_8001', description: 'Failed to feed filament' };

/** A live HMS entry as the status payload enriches it. */
function live(shortCode: string): HMSError {
  return { code: '0x808c', attr: 0x05008000, module: 5, severity: 2, short_code: shortCode };
}

describe('unshownPrinterMessages', () => {
  it('keeps every recorded message when the printer shows nothing live', () => {
    expect(unshownPrinterMessages([PLATE_CHECK, FEED], [])).toEqual([PLATE_CHECK, FEED]);
  });

  it('drops a recorded message the printer still shows — the live summary already carries it', () => {
    expect(unshownPrinterMessages([PLATE_CHECK, FEED], [live('0500_808C')])).toEqual([FEED]);
  });

  it('matches on the code, not on "anything live": a different live fault leaves the record shown', () => {
    expect(unshownPrinterMessages([PLATE_CHECK], [live('0300_400D')])).toEqual([PLATE_CHECK]);
  });

  it('never matches a live entry that carries no short code', () => {
    const bare: HMSError = { code: '0x808c', attr: 0, module: 5, severity: 2 };
    expect(unshownPrinterMessages([PLATE_CHECK], [bare])).toEqual([PLATE_CHECK]);
  });
});

describe('distinctPrinterMessages', () => {
  it('keeps one message per short code, first recorded wins', () => {
    const sibling: PrinterMessage = { short_code: '0500_808C', description: 'other full code, same short code' };
    expect(distinctPrinterMessages([PLATE_CHECK, FEED, sibling])).toEqual([PLATE_CHECK, FEED]);
  });
});

describe('printerMessageText', () => {
  it('is the vendor description when the catalog knows the code', () => {
    expect(printerMessageText(PLATE_CHECK)).toBe('Detected build plate offset or debris');
  });

  it('falls back to the formatted code rather than an empty line', () => {
    expect(printerMessageText({ short_code: '0500_808c', description: '' })).toBe('0500-808C');
  });
});
