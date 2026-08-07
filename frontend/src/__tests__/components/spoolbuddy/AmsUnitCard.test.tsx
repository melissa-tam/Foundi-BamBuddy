/**
 * Tests for AmsUnitCard component:
 * - Renders slot circles for a 4-slot AMS
 * - Shows slot labels (1, 2, 3, 4)
 * - Shows fill level bars
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import React from 'react';
import { AmsUnitCard } from '../../../components/spoolbuddy/AmsUnitCard';
import type { AMSUnit, AMSTray } from '../../../api/client';

// No module mock: `utils/amsHelpers` is the single origin of both the fill-bar
// thresholds and the slot-kind rule this file asserts, so it runs for real (the
// old local getFillBarColor stub was a byte-identical copy of it).

function makeTray(overrides: Partial<AMSTray> = {}): AMSTray {
  return {
    id: 0,
    tray_color: 'FF0000FF',
    tray_type: 'PLA',
    tray_sub_brands: null,
    tray_id_name: null,
    tray_info_idx: null,
    remain: 80,
    k: null,
    cali_idx: null,
    tag_uid: null,
    tray_uuid: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    drying_temp: null,
    drying_time: null,
    ...overrides,
  };
}

function makeUnit(overrides: Partial<AMSUnit> = {}): AMSUnit {
  return {
    id: 0,
    humidity: 30,
    temp: 25,
    is_ams_ht: false,
    tray: [
      makeTray({ id: 0, tray_color: 'FF0000FF', tray_type: 'PLA', remain: 80 }),
      makeTray({ id: 1, tray_color: '00FF00FF', tray_type: 'PETG', remain: 50 }),
      makeTray({ id: 2, tray_color: '0000FFFF', tray_type: 'ABS', remain: 10 }),
      // state=9 = firmware-confirmed empty (#1694: vs state=null which would
      // be "spool loaded but unconfigured", labelled "?" in the UI).
      makeTray({ id: 3, tray_color: null, tray_type: '', remain: -1, state: 9 } as Partial<AMSTray> & { state: number }),
    ],
    serial_number: 'AMS001',
    sw_ver: '1.0.0',
    dry_time: 0,
    dry_status: 0,
    dry_sub_status: 0,
    ...overrides,
  };
}

describe('AmsUnitCard', () => {
  it('renders 4 slot positions for a regular AMS', () => {
    const { container } = render(
      <AmsUnitCard unit={makeUnit()} activeSlot={null} />
    );
    // 4 slot numbers should be visible (1, 2, 3, 4)
    expect(screen.getByText('1')).toBeDefined();
    expect(screen.getByText('2')).toBeDefined();
    expect(screen.getByText('3')).toBeDefined();
    expect(screen.getByText('4')).toBeDefined();
    // grid-cols-4 class should be present
    const grid = container.querySelector('.grid-cols-4');
    expect(grid).not.toBeNull();
  });

  it('renders AMS name in header', () => {
    render(<AmsUnitCard unit={makeUnit({ id: 0 })} activeSlot={null} />);
    expect(screen.getByText('AMS A')).toBeDefined();
  });

  it('shows material types for populated slots', () => {
    render(<AmsUnitCard unit={makeUnit()} activeSlot={null} />);
    expect(screen.getByText('PLA')).toBeDefined();
    expect(screen.getByText('PETG')).toBeDefined();
    expect(screen.getByText('ABS')).toBeDefined();
  });

  it('shows "Empty" for firmware-confirmed empty slot (state 9)', () => {
    render(<AmsUnitCard unit={makeUnit()} activeSlot={null} />);
    expect(screen.getByText('Empty')).toBeDefined();
  });

  it('flags a seated-but-unread slot (state 10) as present, never "Empty" (003-H2S / 004-H2S)', () => {
    // A spool inserted mid-print gets no auto-read; bambu_mqtt promotes it 9→10
    // ("present, not fed"). state 10 means a spool IS seated, so it must render
    // its own distinct state — a labelled "?" ring plus the present caption —
    // and never "Empty" (004-H2S hid a ~90 % roll behind an empty-looking slot).
    const unit = makeUnit({
      tray: [
        makeTray({ id: 0, tray_type: 'PLA', remain: 80 }),
        makeTray({ id: 1, tray_type: 'ABS', remain: 10 }),
        makeTray({ id: 2, tray_type: 'PETG', remain: 50 }),
        makeTray({ id: 3, tray_color: null, tray_type: '', remain: -1, state: 10 } as Partial<AMSTray> & { state: number }),
      ],
    });
    render(<AmsUnitCard unit={unit} activeSlot={null} />);

    // The ring is an accessible image (state is never carried by colour alone).
    const ring = screen.getByRole('img');
    const label = ring.getAttribute('aria-label');
    expect(label).toBeTruthy();
    // A raw key here would mean a missing locale entry.
    expect(label).not.toBe('ams.slotPresentUnread');
    expect(ring.textContent).toBe('?');
    // ...and the caption carries the same string instead of "Empty".
    expect(screen.getAllByText(label as string).length).toBeGreaterThan(0);
    expect(screen.queryByText('Empty')).toBeNull();
  });

  it('shows "?" for loaded-but-unconfigured slot (#1694)', () => {
    // No state reported by firmware + empty tray_type = spool loaded into the
    // slot but no material assigned. Reporter on a 3-AMS P1S saw these slots
    // mislabelled as "Empty" because the prior logic only checked tray_type.
    const unit = makeUnit({
      tray: [
        makeTray({ id: 0, tray_type: 'PLA', remain: 80 }),
        makeTray({ id: 1, tray_color: null, tray_type: '', remain: -1 } as Partial<AMSTray> & { state?: number }),
        makeTray({ id: 2, tray_type: 'ABS', remain: 10 }),
        makeTray({ id: 3, tray_color: null, tray_type: '', remain: -1, state: 9 } as Partial<AMSTray> & { state: number }),
      ],
    });
    render(<AmsUnitCard unit={unit} activeSlot={null} />);
    expect(screen.getByText('?')).toBeDefined();
    // The firmware-empty slot still reads "Empty" — the two states are visually
    // distinct, not collapsed.
    expect(screen.getByText('Empty')).toBeDefined();
  });

  it('renders fill level bars for slots with filament', () => {
    const { container } = render(
      <AmsUnitCard unit={makeUnit()} activeSlot={null} />
    );
    // Look for fill bar elements (they have style width set to fill%)
    const fillBars = container.querySelectorAll('.h-full.rounded-full.transition-all');
    // 3 populated slots should have fill bars (slot 4 is empty)
    expect(fillBars.length).toBe(3);
  });

  it('renders only 1 slot for AMS-HT', () => {
    const htUnit = makeUnit({
      is_ams_ht: true,
      tray: [makeTray({ id: 0, tray_type: 'PLA', remain: 90 })],
    });
    const { container } = render(
      <AmsUnitCard unit={htUnit} activeSlot={null} />
    );
    const grid = container.querySelector('.grid-cols-1');
    expect(grid).not.toBeNull();
    expect(screen.getByText('1')).toBeDefined();
  });

  it('shows humidity and temperature indicators', () => {
    render(<AmsUnitCard unit={makeUnit({ humidity: 45, temp: 30 })} activeSlot={null} />);
    expect(screen.getByText('45%')).toBeDefined();
  });

  it('highlights active slot with ring', () => {
    const { container } = render(
      <AmsUnitCard unit={makeUnit()} activeSlot={1} />
    );
    const activeSlot = container.querySelector('.ring-2.ring-bambu-green');
    expect(activeSlot).not.toBeNull();
  });
});
