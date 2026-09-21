/**
 * Every eject refusal the backend can send reaches the operator as a SENTENCE.
 *
 * The defect this pins: `z_unreferenced` shipped on the backend — the printer
 * rebooted with a part on the plate, so its Z datum is fiction and no sweep may
 * run — and the frontend's code→copy map had no case for it. The operator got
 * the generic "failed to send command" toast, on the one refusal whose whole
 * point is to tell a human to walk over, lift the part off and mark the plate
 * cleared. Nothing failed; the map was simply silent.
 *
 * The compiler now refuses a missing case (`ejectRefusalCopy` switches over the
 * mirrored union with a `never` default), and this file closes the other half
 * of the hole: that the key each case names actually EXISTS in `en.ts`. A case
 * pointing at a leaf nobody wrote renders the raw key, which is the same defect
 * wearing a different coat. The other ten locales are covered by the parity
 * gate, which demands an identical leaf set.
 */
import { describe, expect, it } from 'vitest';
import de from '../../i18n/locales/de';
import en from '../../i18n/locales/en';
import es from '../../i18n/locales/es';
import fr from '../../i18n/locales/fr';
import it_ from '../../i18n/locales/it';
import ja from '../../i18n/locales/ja';
import ko from '../../i18n/locales/ko';
import ptBR from '../../i18n/locales/pt-BR';
import tr from '../../i18n/locales/tr';
import zhCN from '../../i18n/locales/zh-CN';
import zhTW from '../../i18n/locales/zh-TW';
import {
  EJECT_EXTRA_CODES,
  EJECT_REFUSAL_CODES,
  ejectRefusalCopy,
} from '../../hooks/useEjectPlate';

/** Resolve a dotted i18n key against the real English locale. */
const lookup = (dotted: string): unknown =>
  dotted.split('.').reduce<unknown>((node, part) => {
    if (node === null || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, en);

const copyFor = (code: string, detail: Record<string, unknown> = {}) =>
  ejectRefusalCopy(code, detail, 'fallback');

describe('ejectRefusalCopy', () => {
  it('mirrors the backend union exactly, in its own spelling', () => {
    // `EjectRefusalReason` in `backend/app/schemas/printer.py`. Kept by hand —
    // there is no generated client — so the list is stated here too and a
    // divergence is a failing test rather than a silent generic toast.
    expect([...EJECT_REFUSAL_CODES]).toEqual([
      'job_active',
      'dispatch_in_flight',
      'eject_in_flight',
      'z_unreferenced',
      'not_connected',
      'no_plate_gate',
      'bed_unreadable',
      'first_article',
      'no_donor',
      'not_found',
      'profile_not_found',
    ]);
  });

  it('answers EVERY refusal code with a leaf that exists', () => {
    const unanswered = EJECT_REFUSAL_CODES.filter((code) => copyFor(code) === null);
    expect(unanswered).toEqual([]);

    const missingLeaf = EJECT_REFUSAL_CODES.filter(
      (code) => typeof lookup(copyFor(code)!.key) !== 'string',
    );
    expect(missingLeaf).toEqual([]);
  });

  it('answers the codes outside the closed union too', () => {
    const unanswered = EJECT_EXTRA_CODES.filter((code) => copyFor(code) === null);
    expect(unanswered).toEqual([]);
    const missingLeaf = EJECT_EXTRA_CODES.filter(
      (code) => typeof lookup(copyFor(code)!.key) !== 'string',
    );
    expect(missingLeaf).toEqual([]);
  });

  it('gives the Z-reference refusal its own sentence, not the generic toast', () => {
    const copy = copyFor('z_unreferenced');
    expect(copy?.key).toBe('printers.eject.error.zUnreferenced');
    const sentence = lookup(copy!.key);
    expect(typeof sentence).toBe('string');
    // It has to tell the operator to clear the plate BY HAND, and to name the
    // control they will press — the same words the button carries.
    expect(sentence).toContain(en.printers.plateStatus.markCleared);
  });

  it('names the printer, not the profile, when the printer is gone', () => {
    // `not_found` was unhandled and fell through to the generic toast beside
    // `profile_not_found`, which was handled — two 404s, one of them mute.
    expect(copyFor('not_found')?.key).toBe('printers.eject.error.printerNotFound');
    expect(copyFor('profile_not_found')?.key).toBe('printers.eject.error.profileNotFound');
  });

  it('folds the pre-authority spellings onto their current tokens', () => {
    expect(copyFor('printer_busy')?.key).toBe(copyFor('job_active')?.key);
    expect(copyFor('no_eligible_unit')?.key).toBe(copyFor('no_donor')?.key);
  });

  it('tells a started sweep from an unacknowledged one, with and without an age', () => {
    expect(copyFor('eject_in_flight', { started: true, age_s: 12.4 })).toEqual({
      key: 'printers.eject.error.ejectInFlightStarted',
      params: { age: 12 },
    });
    expect(copyFor('eject_in_flight', { started: false, age_s: 12.4 })).toEqual({
      key: 'printers.eject.error.ejectInFlightPending',
      params: { age: 12 },
    });
    // `age_s` is nullable, and `Number(null)` is a finite 0 — the type is the
    // test, so a null age reaches the no-age copy rather than claiming "0 s".
    expect(copyFor('eject_in_flight', { started: true, age_s: null })?.key).toBe(
      'printers.eject.error.ejectInFlightStartedNoAge',
    );
    expect(copyFor('eject_in_flight', { started: false })?.key).toBe(
      'printers.eject.error.ejectInFlightPendingNoAge',
    );
  });

  it("carries the dispatcher’s own guard text through, and falls back when it has none", () => {
    expect(copyFor('eject_dispatch_failed', { message: 'sweep exceeds Y envelope' })).toEqual({
      key: 'printers.eject.error.dispatchFailed',
      params: { message: 'sweep exceeds Y envelope' },
    });
    expect(copyFor('eject_dispatch_failed', {})?.params).toEqual({ message: 'fallback' });
  });

  /**
   * The refusal tells the operator to press a button. It has to call that
   * button what the button calls itself — in EVERY language, not just English.
   *
   * A translator working from the English sentence alone will happily coin a
   * second name for the verb ("clear the plate", "mark the bed empty"), and the
   * operator then hunts a control that does not exist under that name. The
   * parity gate cannot catch it: the leaf is present and translated, it just
   * names the wrong thing. So the sentence is required to CONTAIN the locale's
   * own `printers.plateStatus.markCleared` — the value the button renders.
   */
  it('names the plate-clearing button exactly as that locale renders it', () => {
    const locales = { de, en, es, fr, it: it_, ja, ko, 'pt-BR': ptBR, tr, 'zh-CN': zhCN, 'zh-TW': zhTW };
    const wrong: string[] = [];
    for (const [code, bundle] of Object.entries(locales)) {
      const printers = (bundle as typeof en).printers;
      const sentence = printers.eject.error.zUnreferenced;
      const button = printers.plateStatus.markCleared;
      if (!sentence.includes(button)) wrong.push(`${code}: "${sentence}" lacks "${button}"`);
    }
    expect(wrong).toEqual([]);
  });

  it('returns null for a code this build has never heard of', () => {
    // The runtime fallback the caller turns into the generic failure — kept
    // only for codes OUTSIDE the union, never for a member of it.
    expect(copyFor('a_code_from_a_newer_server')).toBeNull();
    expect(copyFor('')).toBeNull();
  });
});
