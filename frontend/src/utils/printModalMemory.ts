/**
 * Per-file "remember my last choices" persistence for the PrintModal (plan 2c).
 *
 * Stores the operator's last CREATE-mode selections keyed by the file being
 * printed, so re-opening the modal for the same library file / archive
 * pre-fills the eject profile, print options, schedule flags, assignment and
 * quantity instead of re-asking every time.
 *
 * NOT persisted here: AMS manual slot mappings. Slot contents (which spool sits
 * in which tray) go stale between sessions, so a remembered mapping would point
 * at the wrong spool. Manual mappings only carry over on a same-context requeue
 * (plan 2a), where the item's printer/plate context is known-good.
 */
import type { AssignmentMode, PrintOptions } from '../components/PrintModal/types';

/** localStorage key prefix; the caller appends `<libraryFileId|archive:archiveId>`. */
export const STORAGE_KEY_PREFIX = 'bambuddy.printmodal.last.';

/** Bump when the persisted shape changes — older/mismatched blobs read as null. */
const SCHEMA_VERSION = 1;

export interface PrintModalMemory {
  v: typeof SCHEMA_VERSION;
  ejectProfileId: number | null;
  printOptions: PrintOptions;
  requireManualStart: boolean;
  requirePreviousSuccess: boolean;
  autoOffAfter: boolean;
  gcodeInjection: boolean;
  assignmentMode: AssignmentMode;
  targetModel: string | null;
  quantity: number;
}

function storageKey(fileKey: string): string {
  return `${STORAGE_KEY_PREFIX}${fileKey}`;
}

function isPrintModalMemory(value: unknown): value is PrintModalMemory {
  if (typeof value !== 'object' || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    v.v === SCHEMA_VERSION &&
    (v.ejectProfileId === null || typeof v.ejectProfileId === 'number') &&
    typeof v.printOptions === 'object' &&
    v.printOptions !== null &&
    typeof v.requireManualStart === 'boolean' &&
    typeof v.requirePreviousSuccess === 'boolean' &&
    typeof v.autoOffAfter === 'boolean' &&
    typeof v.gcodeInjection === 'boolean' &&
    (v.assignmentMode === 'printer' || v.assignmentMode === 'model') &&
    (v.targetModel === null || typeof v.targetModel === 'string') &&
    typeof v.quantity === 'number'
  );
}

/**
 * Read the remembered choices for a file. Returns null when nothing is stored,
 * the blob is corrupt, or its schema version doesn't match — never throws.
 */
export function readPrintModalMemory(fileKey: string): PrintModalMemory | null {
  try {
    const raw = window.localStorage.getItem(storageKey(fileKey));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isPrintModalMemory(parsed) ? parsed : null;
  } catch {
    // localStorage unavailable (private mode) or JSON.parse failed.
    return null;
  }
}

/**
 * Persist the operator's choices for a file. The schema version is stamped
 * here so callers pass only the payload. Silently no-ops if storage is
 * unavailable — a lost convenience default must never break a submission.
 */
export function writePrintModalMemory(
  fileKey: string,
  data: Omit<PrintModalMemory, 'v'>,
): void {
  try {
    const payload: PrintModalMemory = { v: SCHEMA_VERSION, ...data };
    window.localStorage.setItem(storageKey(fileKey), JSON.stringify(payload));
  } catch {
    // Swallow: storage may be full or blocked; the print still went through.
  }
}
