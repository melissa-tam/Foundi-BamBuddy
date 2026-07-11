/**
 * Tests for hmsTone — honest error/warning/ok tone for a printer's HMS set.
 *
 * The key case (H2): a print_error is hardcoded severity 3 on the backend, so a
 * fatal print-ending error would never be red on severity alone. hmsTone folds
 * gcode_state in — FAILED + any error => 'error'.
 */
import { describe, it, expect } from 'vitest';
import { hmsTone } from '../../utils/hmsTone';
import type { HMSError } from '../../api/client';

function err(severity: number): HMSError {
  return { code: '0x8061', attr: 0x0500_8061, module: 5, severity };
}

describe('hmsTone', () => {
  it('returns "ok" for an empty error set', () => {
    expect(hmsTone([], 'RUNNING')).toBe('ok');
  });

  it('returns "ok" for null/undefined errors', () => {
    expect(hmsTone(null, 'FAILED')).toBe('ok');
    expect(hmsTone(undefined, 'FAILED')).toBe('ok');
  });

  it('returns "error" for a FAILED print carrying a warning-severity error (H2)', () => {
    // severity 3 (a print_error) + FAILED state => red, without any severity remap.
    expect(hmsTone([err(3)], 'FAILED')).toBe('error');
  });

  it('returns "warning" for a PAUSE with a warning-severity error (sev 3)', () => {
    expect(hmsTone([err(3)], 'PAUSE')).toBe('warning');
  });

  it('returns "error" for a fatal code (severity 1) while RUNNING', () => {
    expect(hmsTone([err(1)], 'RUNNING')).toBe('error');
  });

  it('returns "error" for a serious code (severity 2) in any state', () => {
    expect(hmsTone([err(2)], 'IDLE')).toBe('error');
  });

  it('returns "warning" for warning/info codes with no severe code and not FAILED', () => {
    expect(hmsTone([err(3), err(4)], 'RUNNING')).toBe('warning');
  });
});
