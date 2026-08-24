/**
 * Tests for printingUnitPrinters — decoding the `*_has_printing_units` 409.
 *
 * The backend refuses a destructive delete that would reach rows a live print
 * still depends on, and names the printers in a structured detail. Two pages
 * render that refusal (production-run delete, user delete), which is why the
 * expected code is a parameter rather than baked in.
 *
 * The contract this pins: the decision is made on the stable `code`, never on
 * the backend's English sentence, and "no usable names" collapses to null so a
 * caller has ONE falsy check before it tries to render an operator sentence.
 */
import { describe, it, expect } from 'vitest';
import { ApiError } from '../../api/client';
import { printingUnitPrinters } from '../../utils/printingUnitsRefusal';

const CODE = 'user_has_printing_units';

/** A 409 shaped the way the backend sends it. */
function refusal(detail: Record<string, unknown>, code: string = CODE): ApiError {
  return new ApiError('This user still has units printing on 001-H2S.', 409, code, detail);
}

describe('printingUnitPrinters', () => {
  it('returns the printer names when the code matches', () => {
    const error = refusal({ printers: ['001-H2S', '002-H2S'] });
    expect(printingUnitPrinters(error, CODE)).toEqual(['001-H2S', '002-H2S']);
  });

  it('is parameterised by code — the sibling run refusal decodes with its own', () => {
    const error = refusal({ printers: ['003-H2S'] }, 'run_has_printing_units');
    expect(printingUnitPrinters(error, 'run_has_printing_units')).toEqual(['003-H2S']);
  });

  it('returns null for a different code, even with a usable printers list', () => {
    const error = refusal({ printers: ['001-H2S'] }, 'run_has_printing_units');
    expect(printingUnitPrinters(error, CODE)).toBeNull();
  });

  it('returns null when the error carries no code at all', () => {
    const error = new ApiError('Internal Server Error', 500);
    expect(printingUnitPrinters(error, CODE)).toBeNull();
  });

  it('returns null for a plain Error — never matched on the message', () => {
    // The English sentence alone must not be enough to trigger the render:
    // a non-ApiError carrying identical text is still not the refusal.
    const error = new Error('This user still has units printing on 001-H2S.');
    expect(printingUnitPrinters(error, CODE)).toBeNull();
  });

  it('returns null for a null error (no mutation attempted yet)', () => {
    expect(printingUnitPrinters(null, CODE)).toBeNull();
  });

  it('returns null when detail has no printers key', () => {
    expect(printingUnitPrinters(refusal({ message: 'nope' }), CODE)).toBeNull();
  });

  it('returns null when detail is absent entirely', () => {
    const error = new ApiError('Refused', 409, CODE);
    expect(printingUnitPrinters(error, CODE)).toBeNull();
  });

  it('returns null when printers is not an array', () => {
    expect(printingUnitPrinters(refusal({ printers: '001-H2S' }), CODE)).toBeNull();
    expect(printingUnitPrinters(refusal({ printers: 3 }), CODE)).toBeNull();
    expect(printingUnitPrinters(refusal({ printers: null }), CODE)).toBeNull();
    expect(printingUnitPrinters(refusal({ printers: { name: '001-H2S' } }), CODE)).toBeNull();
  });

  it('returns null for an empty printers array', () => {
    // A refusal that names nobody cannot be rendered as a useful sentence, so
    // it collapses to the same null the caller already handles.
    expect(printingUnitPrinters(refusal({ printers: [] }), CODE)).toBeNull();
  });

  it('returns null when the array holds no strings', () => {
    expect(printingUnitPrinters(refusal({ printers: [1, 2, null] }), CODE)).toBeNull();
  });

  it('keeps only the strings from a mixed array', () => {
    const error = refusal({ printers: ['001-H2S', 7, null, '002-H2S', { id: 3 }] });
    expect(printingUnitPrinters(error, CODE)).toEqual(['001-H2S', '002-H2S']);
  });
});
