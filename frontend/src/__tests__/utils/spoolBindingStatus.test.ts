/**
 * The one binding-status derivation shared by the printer card's
 * EmptySlotHoverCard and the Inventory LOCATION column (W5a/W5b).
 *
 * The precedence rule is the part worth pinning: `spent` outranks
 * `preConfigured`, because `pre_configured_at` is a bind-time stamp that an
 * insert-then-run-dry cycle never clears.
 */

import { describe, it, expect } from 'vitest';
import { resolveSpoolBindingStatus } from '../../utils/spoolBindingStatus';

describe('resolveSpoolBindingStatus', () => {
  it('reports a stale claim when neither stamp applies', () => {
    const s = resolveSpoolBindingStatus({ spent: false, preConfigured: false });
    expect(s.id).toBe('notInserted');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.notInserted');
  });

  it('reports awaiting-insert for a deliberate bind-to-empty', () => {
    const s = resolveSpoolBindingStatus({ spent: false, preConfigured: true });
    expect(s.id).toBe('awaitingInsert');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.awaitingInsert');
  });

  it('reports ran-out for a spent roll', () => {
    const s = resolveSpoolBindingStatus({ spent: true, preConfigured: false });
    expect(s.id).toBe('ranOut');
    expect(s.i18nKey).toBe('ams.emptySlotBinding.ranOut');
  });

  it('lets spent outrank pre-configured when both are set', () => {
    const s = resolveSpoolBindingStatus({ spent: true, preConfigured: true });
    expect(s.id).toBe('ranOut');
  });

  it('pairs every state with a distinct icon so colour is never the only cue', () => {
    const states = [
      resolveSpoolBindingStatus({ spent: false, preConfigured: false }),
      resolveSpoolBindingStatus({ spent: false, preConfigured: true }),
      resolveSpoolBindingStatus({ spent: true, preConfigured: false }),
    ];
    const icons = new Set(states.map(s => s.Icon));
    expect(icons.size).toBe(3);
    for (const s of states) {
      expect(s.className).toBeTruthy();
      expect(s.Icon).toBeTruthy();
    }
  });
});
