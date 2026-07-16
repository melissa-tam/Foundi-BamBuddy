/**
 * Tests for formatHmsCode — display formatting of an HMS fault's canonical hex
 * identifier. 16-hex hms[]-array faults render as four hyphen-groups, 8-hex
 * print_error faults as two; malformed/absent full codes fall back to the short
 * code, and an unusable pair yields "".
 */
import { describe, it, expect } from 'vitest';
import { formatHmsCode } from '../../utils/hmsCode';

describe('formatHmsCode', () => {
  it('groups a 16-hex full code into four 4-char groups', () => {
    expect(formatHmsCode('0500010000030004', '0500_0004')).toBe('0500-0100-0003-0004');
  });

  it('groups an 8-hex full code into two 4-char groups', () => {
    expect(formatHmsCode('05000004', '0500_0004')).toBe('0500-0004');
  });

  it('uppercases lowercase hex input', () => {
    expect(formatHmsCode('0500abcd0003def0')).toBe('0500-ABCD-0003-DEF0');
  });

  it('falls back to the short code (as MMMM-CCCC) when full code is absent', () => {
    expect(formatHmsCode(undefined, '0300_400C')).toBe('0300-400C');
  });

  it('falls back to the short code when full code is malformed (wrong length)', () => {
    expect(formatHmsCode('12345', '0300_400C')).toBe('0300-400C');
  });

  it('falls back to the short code when full code has non-hex chars', () => {
    expect(formatHmsCode('05000004000300GZ', 'FFFF_FFFF')).toBe('FFFF-FFFF');
  });

  it('returns "" when neither a usable full code nor short code is provided', () => {
    expect(formatHmsCode()).toBe('');
    expect(formatHmsCode('', '')).toBe('');
    expect(formatHmsCode(undefined, undefined)).toBe('');
  });
});
