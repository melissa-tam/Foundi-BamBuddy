/**
 * queueFilter: the location/status predicates the queue page filters with.
 *
 * The defect it pins: the old page-local location filter read `printer_id`
 * only, so every unit the scheduler had not placed yet — model pools, printers
 * pools, staged units — vanished the moment an operator picked a location,
 * which is exactly the set they were looking for.
 */
import { describe, it, expect } from 'vitest';
import {
  matchesLocation,
  matchesStatus,
  type FilterPrinter,
  type LocationFilterItem,
} from '../../utils/queueFilter';

const fleet: FilterPrinter[] = [
  { id: 1, location: 'Shop', model: 'H2S' },
  { id: 2, location: 'Office', model: 'H2S' },
  { id: 3, location: 'Shop', model: 'H2C' },
  { id: 4, location: null, model: 'P1S' },
];

function item(overrides: Partial<LocationFilterItem> = {}): LocationFilterItem {
  return {
    printer_id: null,
    target_model: null,
    target_printer_ids: null,
    target_location: null,
    ...overrides,
  };
}

describe('matchesLocation', () => {
  it('matches everything when no location is chosen', () => {
    expect(matchesLocation(item(), '', fleet)).toBe(true);
  });

  it('reads the unit OWN target_location first', () => {
    expect(matchesLocation(item({ target_location: 'Shop' }), 'Shop', fleet)).toBe(true);
    expect(matchesLocation(item({ target_location: 'Office' }), 'Shop', fleet)).toBe(false);
  });

  it('lets target_location win over the pinned printer address', () => {
    // The operator narrowed this unit explicitly; a printer that has since
    // moved does not overrule that.
    const pinnedElsewhere = item({ printer_id: 2, target_location: 'Shop' });
    expect(matchesLocation(pinnedElsewhere, 'Shop', fleet)).toBe(true);
    expect(matchesLocation(pinnedElsewhere, 'Office', fleet)).toBe(false);
  });

  it('reads the pinned printer location', () => {
    expect(matchesLocation(item({ printer_id: 1 }), 'Shop', fleet)).toBe(true);
    expect(matchesLocation(item({ printer_id: 2 }), 'Shop', fleet)).toBe(false);
  });

  it('matches a printers pool on ANY member', () => {
    expect(matchesLocation(item({ target_printer_ids: [2, 3] }), 'Shop', fleet)).toBe(true);
    expect(matchesLocation(item({ target_printer_ids: [2] }), 'Shop', fleet)).toBe(false);
  });

  it('matches a model pool on ANY printer of that model', () => {
    expect(matchesLocation(item({ target_model: 'H2S' }), 'Office', fleet)).toBe(true);
    expect(matchesLocation(item({ target_model: 'H2C' }), 'Office', fleet)).toBe(false);
  });

  it('compares models case-insensitively', () => {
    expect(matchesLocation(item({ target_model: 'h2c' }), 'Shop', fleet)).toBe(true);
  });

  it('never matches an unassigned unit — it targets no place', () => {
    expect(matchesLocation(item(), 'Shop', fleet)).toBe(false);
  });

  it('does not match a printer that has no location', () => {
    expect(matchesLocation(item({ printer_id: 4 }), 'Shop', fleet)).toBe(false);
  });

  it('survives an empty fleet', () => {
    expect(matchesLocation(item({ printer_id: 1 }), 'Shop', [])).toBe(false);
  });
});

describe('matchesStatus', () => {
  it('matches everything when no status is chosen', () => {
    expect(matchesStatus({ status: 'pending' }, '')).toBe(true);
  });

  it('matches the exact status only', () => {
    expect(matchesStatus({ status: 'completed' }, 'completed')).toBe(true);
    expect(matchesStatus({ status: 'completed' }, 'cancelled')).toBe(false);
  });
});
