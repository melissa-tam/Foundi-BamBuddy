/**
 * Regression tests for the printer-status bucketing logic in PrintersPage.tsx.
 *
 * The original bug: a printer in gcode_state="FAILED" with no active HMS errors
 * was counted as a "problem" in the header badge — this is the post-cancel
 * terminal state, not a real fault.
 *
 * Post-Phase-2: bucketing keys off `hmsTone` over the UNFILTERED errors, not a
 * frontend known-code table. FAILED-without-error is still "finished"; but a
 * FAILED print carrying ANY error (known OR unknown) now buckets as "error" —
 * unknown codes are no longer silently dropped (H1). A fatal/serious code
 * (severity <= 2) is "error" in any state.
 *
 * Mirrors classifyPrinterStatus + hmsTone in PrintersPage.tsx / utils/hmsTone.ts
 * — kept as inline copies so this test doesn't need the helpers to be exported.
 */
import { describe, it, expect } from 'vitest';

type Status = {
  connected: boolean;
  state: string | null;
  hms_errors?: { code: string; attr: number; severity: number }[];
};

type Bucket = 'printing' | 'paused' | 'finished' | 'idle' | 'offline' | 'error';

function hmsTone(errors: Status['hms_errors'], gcodeState: string | null): 'error' | 'warning' | 'ok' {
  if (!errors || errors.length === 0) return 'ok';
  const anySevere = errors.some((e) => e.severity <= 2);
  if (anySevere || gcodeState === 'FAILED') return 'error';
  return 'warning';
}

function classifyPrinterStatus(status: Status | undefined): Bucket {
  if (!status?.connected) return 'offline';
  if (hmsTone(status.hms_errors, status.state) === 'error') return 'error';
  switch (status.state) {
    case 'RUNNING': return 'printing';
    case 'PAUSE': return 'paused';
    case 'FINISH': return 'finished';
    case 'FAILED': return 'finished';
    default: return 'idle';
  }
}

describe('FAILED-without-HMS bucketing', () => {
  it('classifies FAILED with no HMS errors as "finished" (post-cancel terminal state, not a problem)', () => {
    const cancelledPrinter: Status = {
      connected: true,
      state: 'FAILED',
      hms_errors: [],
    };
    expect(classifyPrinterStatus(cancelledPrinter)).toBe('finished');
  });

  it('classifies FAILED + active known HMS as "error"', () => {
    const reallyFailedPrinter: Status = {
      connected: true,
      state: 'FAILED',
      hms_errors: [{ code: '0x4057', attr: 0x0300_0000, severity: 1 }],
    };
    expect(classifyPrinterStatus(reallyFailedPrinter)).toBe('error');
  });

  it('classifies FAILED + only unknown HMS as "error" (unfiltered — unknown faults are NOT hidden)', () => {
    const unknownFault: Status = {
      connected: true,
      state: 'FAILED',
      hms_errors: [{ code: '0x2001b', attr: 0x0C00_0C00, severity: 1 }], // 0C00_001B not in any table
    };
    // Was 'finished' pre-Phase-2 (dropped as "unknown"); now a visible problem.
    expect(classifyPrinterStatus(unknownFault)).toBe('error');
  });

  it('classifies RUNNING + warning-severity error (sev 3) as "printing" (tone is warning, not error)', () => {
    const runningWithWarning: Status = {
      connected: true,
      state: 'RUNNING',
      hms_errors: [{ code: '0x8061', attr: 0x0500_8061, severity: 3 }],
    };
    expect(classifyPrinterStatus(runningWithWarning)).toBe('printing');
  });

  it('classifies FINISH as "finished" (unchanged baseline)', () => {
    const completedPrinter: Status = { connected: true, state: 'FINISH' };
    expect(classifyPrinterStatus(completedPrinter)).toBe('finished');
  });

  it('classifies disconnected printer as "offline" (HMS / state irrelevant)', () => {
    const offline: Status = {
      connected: false,
      state: 'FAILED',
      hms_errors: [{ code: '0x4057', attr: 0x0300_0000, severity: 1 }],
    };
    expect(classifyPrinterStatus(offline)).toBe('offline');
  });
});
