import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  STORAGE_KEY_PREFIX,
  readPrintModalMemory,
  writePrintModalMemory,
  type PrintModalMemory,
} from '../../utils/printModalMemory';

// The global test setup stubs window.localStorage with no-op vi.fn()s. Back
// them with a real in-memory store so persistence round-trips like a browser.
function installMemoryLocalStorage(): void {
  const store = new Map<string, string>();
  vi.mocked(window.localStorage.getItem).mockImplementation((k: string) => (store.has(k) ? store.get(k)! : null));
  vi.mocked(window.localStorage.setItem).mockImplementation((k: string, v: string) => { store.set(k, v); });
  vi.mocked(window.localStorage.removeItem).mockImplementation((k: string) => { store.delete(k); });
  vi.mocked(window.localStorage.clear).mockImplementation(() => { store.clear(); });
}

const payload: Omit<PrintModalMemory, 'v'> = {
  ejectProfileId: 7,
  printOptions: {
    bed_levelling: false,
    flow_cali: true,
    vibration_cali: false,
    layer_inspect: true,
    timelapse: true,
    nozzle_offset_cali: false,
  },
  requireManualStart: true,
  requirePreviousSuccess: true,
  autoOffAfter: true,
  gcodeInjection: true,
  assignmentMode: 'model',
  targetModel: 'H2S',
  quantity: 4,
};

describe('printModalMemory', () => {
  beforeEach(() => {
    installMemoryLocalStorage();
  });

  it('round-trips a write then read, stamping the schema version', () => {
    writePrintModalMemory('5', payload);
    const read = readPrintModalMemory('5');
    expect(read).toEqual({ v: 1, ...payload });
  });

  it('keys entries by file so different files do not collide', () => {
    writePrintModalMemory('5', payload);
    writePrintModalMemory('archive:9', { ...payload, ejectProfileId: null, quantity: 1 });

    expect(readPrintModalMemory('5')?.ejectProfileId).toBe(7);
    expect(readPrintModalMemory('archive:9')?.ejectProfileId).toBeNull();
    expect(readPrintModalMemory('archive:9')?.quantity).toBe(1);
  });

  it('preserves an explicit None (null eject profile)', () => {
    writePrintModalMemory('5', { ...payload, ejectProfileId: null });
    // A stored null must be readable as null (distinct from "no memory").
    expect(readPrintModalMemory('5')).not.toBeNull();
    expect(readPrintModalMemory('5')?.ejectProfileId).toBeNull();
  });

  it('returns null when nothing is stored', () => {
    expect(readPrintModalMemory('does-not-exist')).toBeNull();
  });

  it('tolerates corrupt JSON, returning null instead of throwing', () => {
    window.localStorage.setItem(`${STORAGE_KEY_PREFIX}5`, '{ not: valid json');
    expect(readPrintModalMemory('5')).toBeNull();
  });

  it('rejects a well-formed blob whose schema does not match', () => {
    window.localStorage.setItem(`${STORAGE_KEY_PREFIX}5`, JSON.stringify({ v: 1, foo: 'bar' }));
    expect(readPrintModalMemory('5')).toBeNull();
  });

  it('rejects a blob from a different schema version', () => {
    window.localStorage.setItem(
      `${STORAGE_KEY_PREFIX}5`,
      JSON.stringify({ v: 99, ...payload }),
    );
    expect(readPrintModalMemory('5')).toBeNull();
  });
});
