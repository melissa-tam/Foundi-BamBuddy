/**
 * `utils/printResume` — what a Bambuddy resume tells the operator: `refilling`
 * names the slot the farm loads first, a 409 refusal reads its reason's own
 * sentence, and a bulk resume is ONE counted toast. Copy resolves through the
 * real i18n instance (the `en` baseline), and is compared against `en` leaves.
 */
import { describe, expect, it } from 'vitest';
import i18n from '../../i18n';
import en from '../../i18n/locales/en';
import { ApiError } from '../../api/client';
import type { PrintResumeResponse, ResumeRefusalReason } from '../../api/client';
import {
  RESUME_REFUSAL_KEY,
  bulkResumeToast,
  refillingToast,
  refusalToast,
  resumeRefusalOf,
  tallyResumes,
} from '../../utils/printResume';

const t = (key: string, options?: Record<string, unknown>) => i18n.t(key, options);

function refusalError(detail: Record<string, unknown> | null, status = 409): ApiError {
  return new ApiError(typeof detail?.message === 'string' ? detail.message : 'HTTP 409', status, null, detail);
}

const REASONS: Record<ResumeRefusalReason, true> = {
  not_paused: true,
  farm_acting: true,
  maintenance: true,
  unknown: true,
  command_pending: true,
  physical: true,
};

const resumed: PrintResumeResponse = { success: true, status: 'resumed', slot: null, message: 'sent' };
const refilling: PrintResumeResponse = { success: true, status: 'refilling', slot: 'AMS A slot 1', message: 'x' };

describe('refillingToast', () => {
  it('names the slot the farm loads, as info', () => {
    expect(refillingToast('AMS A slot 1', t)).toEqual({
      message: en.printers.toast.resumeRefilling.replace('{{slot}}', 'AMS A slot 1'),
      type: 'info',
    });
  });

  it('says "a spool" when the selection picks the slot', () => {
    expect(refillingToast(null, t).message).toBe(en.printers.toast.resumeRefillingAnySlot);
  });
});

describe('resumeRefusalOf + refusalToast', () => {
  it.each(Object.keys(REASONS) as ResumeRefusalReason[])('reads the %s refusal in its own words', (reason) => {
    const refusal = resumeRefusalOf(refusalError({ reason, slot: null, answer: null, message: 'server' }));
    expect(refusal).toEqual({ reason, slot: null, answer: null, message: 'server' });
    const toast = refusalToast(refusal!, t);
    expect(toast.type).toBe('error');
    expect(toast.message).toBe(en.printers.toast.resumeRefused[reason]);
  });

  it('has a sentence for exactly the closed reason set', () => {
    expect(Object.keys(RESUME_REFUSAL_KEY).sort()).toEqual(Object.keys(REASONS).sort());
    expect(Object.keys(en.printers.toast.resumeRefused).sort()).toEqual(Object.keys(REASONS).sort());
  });

  it('is no refusal for a reason this build does not know (the server sentence is the fallback)', () => {
    expect(resumeRefusalOf(refusalError({ reason: 'newer', message: 'server' }))).toBeNull();
  });

  it('is no refusal for any other error', () => {
    expect(resumeRefusalOf(new ApiError('Printer not connected', 400))).toBeNull();
    expect(resumeRefusalOf(refusalError({ reason: 'physical', message: 'server' }, 502))).toBeNull();
    expect(resumeRefusalOf(refusalError(null))).toBeNull();
    expect(resumeRefusalOf(new Error('network'))).toBeNull();
  });
});

describe('bulk resume', () => {
  const refused = (reason: ResumeRefusalReason): PromiseSettledResult<PrintResumeResponse> => ({
    status: 'rejected',
    reason: refusalError({ reason, slot: null, answer: null, message: 'server' }),
  });
  const ok = (value: PrintResumeResponse): PromiseSettledResult<PrintResumeResponse> => ({ status: 'fulfilled', value });
  const failed: PromiseSettledResult<PrintResumeResponse> = {
    status: 'rejected',
    reason: new ApiError('Printer not connected', 400),
  };

  it('counts each answer in its own bucket', () => {
    expect(tallyResumes([ok(resumed), ok(refilling), ok(refilling), refused('physical'), failed])).toEqual({
      resumed: 1,
      refilling: 2,
      refused: 1,
      failed: 1,
    });
  });

  it('says all of it in ONE toast, singular and plural, and errors when any printer was refused', () => {
    const toast = bulkResumeToast({ resumed: 2, refilling: 1, refused: 1, failed: 0 }, t);
    expect(toast.type).toBe('error');
    expect(toast.message).toBe(
      [
        en.printers.bulk.resumeResumed_other.replace('{{count}}', '2'),
        en.printers.bulk.resumeRefilling_one.replace('{{count}}', '1'),
        en.printers.bulk.resumeRefused_one.replace('{{count}}', '1'),
      ].join(' '),
    );
  });

  it('is info while printers are still refilling and nothing failed', () => {
    const toast = bulkResumeToast({ resumed: 1, refilling: 3, refused: 0, failed: 0 }, t);
    expect(toast.type).toBe('info');
    expect(toast.message).toContain(en.printers.bulk.resumeRefilling_other.replace('{{count}}', '3'));
  });

  it('is success when every resume went out, and names no empty bucket', () => {
    const toast = bulkResumeToast({ resumed: 1, refilling: 0, refused: 0, failed: 0 }, t);
    expect(toast).toEqual({ message: en.printers.bulk.resumeResumed_one.replace('{{count}}', '1'), type: 'success' });
  });

  it('counts a resume that was not delivered as failed, not refused', () => {
    const toast = bulkResumeToast(tallyResumes([failed, failed]), t);
    expect(toast.type).toBe('error');
    expect(toast.message).toBe(en.printers.bulk.resumeFailed_other.replace('{{count}}', '2'));
  });
});
