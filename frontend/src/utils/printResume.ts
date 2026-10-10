/**
 * THE owner of what a Bambuddy resume of a paused print tells the operator — the
 * printer card's Resume, the printers page's bulk Resume and the HMS dialog's resume
 * buttons all answer through here.
 *
 * Since 2026-10-10 a resume over an EMPTY toolhead is not sent as-is: the backend
 * (`spool_recovery.resume_paused_print`) loads the print's spool first and resumes once
 * the load reached the toolhead (Raymond: "when i click resume there MUST be filament
 * loaded"). So a resume answers one of three ways, and each gets its own sentence:
 *   - sent      — the surface's existing success toast (its own copy, not this module's)
 *   - refilling — "Toolhead empty. Loading AMS A slot 1, then resuming." (`refillingToast`)
 *   - refused   — a 409 with a closed `reason`, one localized sentence per reason
 *                 (`resumeRefusalOf` + `refusalToast`); a reason this build does not know
 *                 is left to the caller's generic error path, which prints the server's
 *                 own `message`.
 *
 * Copy is keyed off `status` / `reason`, never off the backend's English `message`.
 */
import { ApiError } from '../api/client';
import type { PrintResumeRefusal, PrintResumeResponse, ResumeRefusalReason } from '../api/client';
import type { ToastType } from '../contexts/ToastContext';

/** The translator these helpers resolve copy through (`useTranslation().t`). */
export type Translate = (key: string, options?: Record<string, unknown>) => string;

/** A resolved toast: the sentence and its variant, ready for `showToast`. */
export interface ResumeToast {
  message: string;
  type: ToastType;
}

/** One sentence per refusal reason. A `Record`, so a reason added to the union without copy fails `tsc -b`. */
export const RESUME_REFUSAL_KEY: Record<ResumeRefusalReason, string> = {
  not_paused: 'printers.toast.resumeRefused.not_paused',
  farm_acting: 'printers.toast.resumeRefused.farm_acting',
  maintenance: 'printers.toast.resumeRefused.maintenance',
  unknown: 'printers.toast.resumeRefused.unknown',
  command_pending: 'printers.toast.resumeRefused.command_pending',
  physical: 'printers.toast.resumeRefused.physical',
};

function isRefusalReason(value: unknown): value is ResumeRefusalReason {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(RESUME_REFUSAL_KEY, value);
}

/**
 * The refusal behind a resume's error, or `null` for any other error (not a 409, not
 * the refusal shape, or a reason this build has no sentence for).
 */
export function resumeRefusalOf(error: unknown): PrintResumeRefusal | null {
  if (!(error instanceof ApiError) || error.status !== 409 || error.detail === null) return null;
  const { reason, slot, answer, message } = error.detail;
  if (!isRefusalReason(reason) || typeof message !== 'string') return null;
  return {
    reason,
    slot: typeof slot === 'string' ? slot : null,
    answer: typeof answer === 'string' ? answer : null,
    message,
  };
}

/** The error toast of a refused resume: the reason's own sentence. */
export function refusalToast(refusal: PrintResumeRefusal, t: Translate): ResumeToast {
  return { message: t(RESUME_REFUSAL_KEY[refusal.reason]), type: 'error' };
}

/**
 * The toast of a resume answered `refilling`: the farm loads first, then resumes. Info,
 * not success — the resume has not happened yet; its outcome is the card's
 * "Toolhead empty" chip, and a page on failure.
 */
export function refillingToast(slot: string | null, t: Translate): ResumeToast {
  return {
    message: slot
      ? t('printers.toast.resumeRefilling', { slot })
      : t('printers.toast.resumeRefillingAnySlot'),
    type: 'info',
  };
}

/** How N independent resume calls ended, counted per answer. */
export interface ResumeTally {
  resumed: number;
  refilling: number;
  refused: number;
  /** Not answered with a status or a refusal: not connected, not delivered, a network error. */
  failed: number;
}

/** Count settled `api.resumePrint` calls by what each answered. */
export function tallyResumes(results: readonly PromiseSettledResult<PrintResumeResponse>[]): ResumeTally {
  const tally: ResumeTally = { resumed: 0, refilling: 0, refused: 0, failed: 0 };
  for (const result of results) {
    if (result.status === 'fulfilled') {
      tally[result.value.status] += 1;
    } else if (resumeRefusalOf(result.reason) !== null) {
      tally.refused += 1;
    } else {
      tally.failed += 1;
    }
  }
  return tally;
}

/** The plural family each tally bucket reads, in reading order. */
const BULK_SENTENCE_KEY: Record<keyof ResumeTally, string> = {
  resumed: 'printers.bulk.resumeResumed',
  refilling: 'printers.bulk.resumeRefilling',
  refused: 'printers.bulk.resumeRefused',
  failed: 'printers.bulk.resumeFailed',
};

const BULK_ORDER: readonly (keyof ResumeTally)[] = ['resumed', 'refilling', 'refused', 'failed'];

/**
 * ONE toast for a bulk resume: a counted sentence per non-empty bucket, never one toast
 * per printer. Error when any printer was refused or failed, info when some are still
 * refilling, success otherwise.
 */
export function bulkResumeToast(tally: ResumeTally, t: Translate): ResumeToast {
  const message = BULK_ORDER.filter((bucket) => tally[bucket] > 0)
    .map((bucket) => t(BULK_SENTENCE_KEY[bucket], { count: tally[bucket] }))
    .join(' ');
  const type: ToastType =
    tally.refused + tally.failed > 0 ? 'error' : tally.refilling > 0 ? 'info' : 'success';
  return { message, type };
}
