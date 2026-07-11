/**
 * Tests for showNoUsbChip — fail-safe gate for the printer-card "No USB drive"
 * chip (#F8).
 *
 * The H2S has no microSD slot, so a missing USB-A drive is reported as
 * sdcard=false and means FTPS 553 on every dispatch. The chip must be
 * fail-SAFE: render ONLY on an explicit reported sdcard===false, never on
 * missing/undefined data, and never before a real status report has landed
 * (backend defaults sdcard=False pre-report; a truthy gcode_state proves a
 * report arrived).
 */
import { describe, it, expect } from 'vitest';
import { showNoUsbChip, type NoUsbChipInput } from '../../utils/noUsbChip';

function status(overrides: Partial<NoUsbChipInput>): NoUsbChipInput {
  return { connected: true, state: 'IDLE', sdcard: false, ...overrides };
}

describe('showNoUsbChip', () => {
  it('renders when connected + real state + sdcard===false', () => {
    expect(showNoUsbChip(status({ sdcard: false }))).toBe(true);
  });

  it('is absent when sdcard===true (drive present)', () => {
    expect(showNoUsbChip(status({ sdcard: true }))).toBe(false);
  });

  it('is absent when sdcard is undefined (missing data — fail-safe)', () => {
    expect(showNoUsbChip(status({ sdcard: undefined }))).toBe(false);
  });

  it('is absent when disconnected even if sdcard===false', () => {
    expect(showNoUsbChip(status({ connected: false, sdcard: false }))).toBe(false);
  });

  it('is absent before the first real report (state null) — guards the default', () => {
    expect(showNoUsbChip(status({ state: null, sdcard: false }))).toBe(false);
    expect(showNoUsbChip(status({ state: '', sdcard: false }))).toBe(false);
  });

  it('is absent for null/undefined status', () => {
    expect(showNoUsbChip(null)).toBe(false);
    expect(showNoUsbChip(undefined)).toBe(false);
  });
});
