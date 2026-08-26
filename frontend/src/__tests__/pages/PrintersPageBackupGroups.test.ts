/**
 * Tests for #1762 — `computeBackupGroups` identity rule.
 *
 * Slots pair ONLY when they share a preset (`tray_info_idx`, falling back to
 * `tray_type`) AND a byte-equal normalised colour, on one extruder side.
 * Nozzle temperature is NOT in the firmware's key — proven 2026-08-25 against
 * the wire's own `filam_bak` groups, and pinned below.
 * Empty slots are skipped; non-empty slots without a peer come back as
 * 1-member entries so the modal can list them as "Slots without a peer".
 */
import { describe, it, expect } from 'vitest';

import { computeBackupGroups, nearMissBackupSlots } from '../../utils/amsHelpers';

function ams(id: number, tray: Array<{
  tray_type?: string | null;
  tray_sub_brands?: string | null;
  tray_color?: string | null;
  tray_info_idx?: string | null;
  nozzle_temp_min?: number | null;
  nozzle_temp_max?: number | null;
}>) {
  return {
    id,
    tray: tray.map((t, i) => ({
      id: i,
      tray_type: t.tray_type ?? null,
      tray_sub_brands: t.tray_sub_brands ?? null,
      tray_color: t.tray_color ?? null,
      tray_info_idx: t.tray_info_idx ?? null,
      nozzle_temp_min: t.nozzle_temp_min ?? null,
      nozzle_temp_max: t.nozzle_temp_max ?? null,
    })),
  };
}

describe('computeBackupGroups', () => {
  it('returns empty list for missing/empty AMS input', () => {
    expect(computeBackupGroups(undefined, {}, false)).toEqual([]);
    expect(computeBackupGroups([], {}, false)).toEqual([]);
  });

  it('skips empty slots entirely', () => {
    const groups = computeBackupGroups(
      [ams(0, [
        { tray_type: null, tray_color: null, tray_info_idx: null },
        { tray_type: null, tray_color: null, tray_info_idx: null },
      ])],
      {},
      false,
    );
    expect(groups).toEqual([]);
  });

  it('groups two slots in different AMS units holding the same preset', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].presetId).toBe('GFA00');
    expect(groups[0].members.map((m) => m.globalTrayId)).toEqual([0, 4]);
  });

  it('falls back to tray_type: two slots with no profile id pair on material + colour', () => {
    // Mirrors the backend `tray_fields.backup_group_key` preset rule
    // (`tray_info_idx or tray_type`). One rule, two spellings — a tray that
    // grouped here but read presetless to the backend would make the badge and
    // the `[spool-select]` backup WARN contradict each other.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#FF0000' }]),
        ams(1, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#FF0000' }]),
      ],
      {},
      false,
    );

    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
    expect(groups[0].presetId).toBe('PLA');
  });

  it('fallback preset is still material-strict: different tray_type never pairs', () => {
    // The fallback widens WHERE the preset comes from, never what counts as a
    // match: two materially-different spools sharing a colour are not backups.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#FF0000' }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '#FF0000' }]),
      ],
      {},
      false,
    );

    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
  });

  it('does NOT group slots with different presets even if same material', () => {
    const groups = computeBackupGroups(
      [ams(0, [
        { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
        { tray_type: 'PLA', tray_color: '#FFFFFF', tray_info_idx: 'GFA01' },
      ])],
      {},
      false,
    );
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
  });

  it('returns lone slots alongside pairs in the same list', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [
          { tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' },
          { tray_type: 'PETG', tray_color: '#0000FF', tray_info_idx: 'GFG99' },
        ]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    // 1 pair + 1 lone, pair first by sort order.
    expect(groups).toHaveLength(2);
    expect(groups[0].members).toHaveLength(2);
    expect(groups[1].members).toHaveLength(1);
    expect(groups[1].displayName).toContain('PETG');
  });

  it('on dual-extruder printers, scopes pairs per extruder side', () => {
    // ams 0 = right (0), ams 1 = left (1). Same preset on different sides:
    // each comes back as a 1-member entry.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      { '0': 0, '1': 1 },
      true,
    );
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
    expect(groups[0].extruder).toBe(0);
    expect(groups[1].extruder).toBe(1);
  });

  it('on dual-extruder printers, pairs slots on the same extruder side', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(2, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      { '0': 0, '1': 1, '2': 0 },
      true,
    );

    // Right-side pair (AMS 0 + 2), left-side lone (AMS 1).
    const rightPair = groups.find((g) => g.extruder === 0 && g.members.length === 2);
    expect(rightPair).toBeDefined();
    expect(rightPair!.members.map((m) => m.globalTrayId).sort((a, b) => a - b)).toEqual([0, 8]);
    const leftLone = groups.find((g) => g.extruder === 1);
    expect(leftLone).toBeDefined();
    expect(leftLone!.members).toHaveLength(1);
  });

  it('handles AMS-HT (single-tray, id >= 128) via getGlobalTrayId — pairs with regular AMS slot', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
        ams(128, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members.map((m) => m.globalTrayId).sort((a, b) => a - b)).toEqual([0, 128]);
  });

  it('STRICT colour rule: same preset, different colours do NOT pair', () => {
    // Reporter screenshot scenario — three PETG HF slots all sharing the
    // same Bambu profile ID (e.g. GFG99) but in three different colours
    // cannot back each other up; the firmware would correctly swap PETG HF
    // but the print would change colour mid-run.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG99' }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '#FF0000', tray_info_idx: 'GFG99' }]),
        ams(2, [{ tray_type: 'PETG', tray_color: '#00FF00', tray_info_idx: 'GFG99' }]),
      ],
      {},
      false,
    );
    // Three lone slots — no pair.
    expect(groups).toHaveLength(3);
    expect(groups.every((g) => g.members.length === 1)).toBe(true);
  });

  it('colour normalisation: 6-char and 8-char hex of the same RGB pair correctly', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_color: '000000', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_color: '000000FF', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
  });

  it('defensively dedupes duplicate ams.id entries (first wins)', () => {
    // Observed in the wild: status.ams sometimes contains the same ams.id
    // twice (VP-aggregated switch printers, MQTT partial-update edge cases).
    // The modal must NOT render the same slot label with conflicting
    // materials — first occurrence wins, second is dropped.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG99' }]),
        ams(0, [{ tray_type: 'PLA', tray_color: '#000000', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].displayName).toContain('PETG');
  });

  it('preserves display name + tray colour from the first slot for the modal swatch', () => {
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#1A1A1A', tray_info_idx: 'GFA00' }]),
        ams(1, [{ tray_type: 'PLA', tray_sub_brands: 'PLA Basic', tray_color: '#1A1A1A', tray_info_idx: 'GFA00' }]),
      ],
      {},
      false,
    );
    expect(groups[0].displayName).toBe('PLA Basic');
    expect(groups[0].trayColor).toBe('#1A1A1A');
  });

  it('PAIRS slots whose nozzle-temperature windows differ — temps are not in the key', () => {
    // The 010-H2S measurement: the firmware reported `filam_bak = [15]` for
    // T0 (RFID-tagged, 230-260) beside T1-T3 (tagless, 230-270) — one group
    // across differing windows. A temps dimension in this key was the false
    // "No backup slot" badge on a slot the firmware had already grouped.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG02', nozzle_temp_min: 230, nozzle_temp_max: 260 }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG02', nozzle_temp_min: 230, nozzle_temp_max: 270 }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
  });

  it('pairs slots that both report no nozzle-temperature window', () => {
    // A missing window is not a missing identity — grouping never depended on
    // the field being reported at all.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG99' }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG99' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
  });

  it('pairs a slot reporting temps with one reporting none', () => {
    // The asymmetry the old temps key created: an RFID roll carrying its tag's
    // window beside a farm-configured tray that reports none is exactly the
    // 010-H2S shape, and the firmware groups them.
    const groups = computeBackupGroups(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG02', nozzle_temp_min: 230, nozzle_temp_max: 260 }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '#000000', tray_info_idx: 'GFG02' }]),
      ],
      {},
      false,
    );
    expect(groups).toHaveLength(1);
    expect(groups[0].members).toHaveLength(2);
  });
});

/**
 * Tests for the 010-H2S near-miss badge: a slot with no firmware backup
 * partner that HAS a same-filament neighbour the firmware excluded on an
 * exact-match colour difference. Colour is the ONLY difference that can
 * survive the scan, which is what the badge copy is allowed to state.
 */
describe('nearMissBackupSlots', () => {
  function nearMiss(
    units: ReturnType<typeof ams>[],
    extruderMap: Record<string, number> = {},
    isDualNozzle = false,
  ): number[] {
    const groups = computeBackupGroups(units, extruderMap, isDualNozzle);
    return [...nearMissBackupSlots(groups, units)].sort((a, b) => a - b);
  }

  it('returns nothing for missing/empty AMS input', () => {
    expect(nearMiss([])).toEqual([]);
    expect([...nearMissBackupSlots([], undefined)]).toEqual([]);
  });

  it('flags both greys the firmware refused to pair (161616FF vs 000000FF)', () => {
    // The incident shape: slot 2 ran dry twice with a full black roll in the
    // AMS. Both slots are the lone member of their own group, and each is the
    // other's near miss — so both light up.
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '161616FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' },
      ]),
    ])).toEqual([0, 1]);
  });

  it('flags NOTHING for a same-colour pair differing only in nozzle temps', () => {
    // The false badge itself: the firmware groups these two (010-H2S T0 beside
    // T1-T3), so neither slot is lone and neither is news.
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG02', nozzle_temp_min: 230, nozzle_temp_max: 260 },
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG02', nozzle_temp_min: 230, nozzle_temp_max: 270 },
      ]),
    ])).toEqual([]);
  });

  it('flags nothing on a multi-colour AMS with no similar neighbour', () => {
    // The anti-noise case: four different colours of one preset must NOT all
    // light up just because none of them has a peer.
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: 'FF0000FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: '00FF00FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: 'FFFFFFFF', tray_info_idx: 'GFG99' },
      ]),
    ])).toEqual([]);
  });

  it('flags nothing when the similar neighbour is a different preset', () => {
    // Different presets are different filaments — no backup was ever possible,
    // so there is nothing actionable to report.
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '161616FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG02' },
      ]),
    ])).toEqual([]);
  });

  it('flags nothing when the similar neighbour is on the other extruder side', () => {
    // The firmware cannot cross extruders even with the global backup bit set.
    expect(nearMiss(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '161616FF', tray_info_idx: 'GFG99' }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' }]),
      ],
      { '0': 0, '1': 1 },
      true,
    )).toEqual([]);
  });

  it('flags the same pair when both sit on the same extruder side', () => {
    expect(nearMiss(
      [
        ams(0, [{ tray_type: 'PETG', tray_color: '161616FF', tray_info_idx: 'GFG99' }]),
        ams(1, [{ tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' }]),
      ],
      { '0': 0, '1': 0 },
      true,
    )).toEqual([0, 4]);
  });

  it('ignores empty slots', () => {
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '161616FF', tray_info_idx: 'GFG99' },
        { tray_type: null, tray_color: null, tray_info_idx: null },
      ]),
    ])).toEqual([]);
  });

  it('flags a fallback-preset pair the firmware split on colour', () => {
    // No profile id on either slot, so both key on `tray_type` — the same
    // preset the backend's WARN would compare. The two greys still land in
    // different groups, and the badge must agree with that WARN.
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '161616FF' },
        { tray_type: 'PETG', tray_color: '000000FF' },
      ]),
    ])).toEqual([0, 1]);
  });

  it('never flags a slot that already has a firmware peer', () => {
    // Slots 0+1 pair; slot 2 is the lone grey beside them and is the only
    // actionable one.
    expect(nearMiss([
      ams(0, [
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: '000000FF', tray_info_idx: 'GFG99' },
        { tray_type: 'PETG', tray_color: '161616FF', tray_info_idx: 'GFG99' },
      ]),
    ])).toEqual([2]);
  });
});
