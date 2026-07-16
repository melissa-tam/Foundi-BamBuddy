/**
 * mapModelCode — SSDP device code → display name.
 *
 * Locks the P-series corrections that were wrong in the old duplicated copies
 * (C11→P1S, C12→P1P, C13→P2S, N7 missing). The corrected mapping mirrors the
 * backend's authoritative VP-code fix map: P1P↔C11, P1S↔C12, X1E↔C13, P2S↔N7.
 */

import { describe, it, expect } from 'vitest';
import { mapModelCode } from '../../utils/printerModels';

describe('mapModelCode', () => {
  it('maps the corrected P-series / X1E SSDP device codes', () => {
    expect(mapModelCode('C11')).toBe('P1P');
    expect(mapModelCode('C12')).toBe('P1S');
    expect(mapModelCode('C13')).toBe('X1E');
    expect(mapModelCode('N7')).toBe('P2S');
  });

  it('maps H2-series SSDP codes to display names', () => {
    expect(mapModelCode('O1S')).toBe('H2S');
    expect(mapModelCode('O1C')).toBe('H2C');
    expect(mapModelCode('O1C2')).toBe('H2C');
    expect(mapModelCode('O1D')).toBe('H2D');
  });

  it('passes display names and unknown codes through unchanged', () => {
    expect(mapModelCode('H2S')).toBe('H2S');
    expect(mapModelCode('P2S')).toBe('P2S');
    expect(mapModelCode('WHO-KNOWS')).toBe('WHO-KNOWS');
  });

  it('returns an empty string for a null model', () => {
    expect(mapModelCode(null)).toBe('');
  });
});
