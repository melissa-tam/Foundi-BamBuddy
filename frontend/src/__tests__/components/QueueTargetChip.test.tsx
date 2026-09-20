/**
 * QueueTargetChip: the ONE rendering of "what does this row dispatch against".
 *
 * It is extracted from the unit row so the collapsed run row can state the same
 * fact in the same shape, so what it must keep is the unit row's reading — the
 * pool suffixes a pool target carries, the bare label a pinned or unassigned
 * target carries, and the full text in the tooltip because the label truncates.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import { render } from '../utils';
import { QueueTargetChip } from '../../components/QueueTargetChip';
import type { QueueTargetChipItem } from '../../components/QueueTargetChip';

// Echo the key so labels read as `<key> <names>` without the i18n runtime.
const t = (k: string) => k;

const printerNameById = new Map<number, string>([
  [1, 'H2S-Alpha'],
  [2, 'H2C-Beta'],
]);

function item(overrides: Partial<QueueTargetChipItem> = {}): QueueTargetChipItem {
  return {
    printer_id: null,
    printer_name: null,
    target_model: null,
    target_printer_ids: null,
    target_location: null,
    required_filament_types: null,
    ...overrides,
  };
}

/** The chip's label node — the one carrying the tooltip. */
const label = (text: string) => screen.getByTitle(text);

describe('QueueTargetChip', () => {
  afterEach(cleanup);

  it('names a pinned printer, bare', () => {
    render(
      <QueueTargetChip
        item={item({ printer_id: 2, printer_name: 'H2C-Beta' })}
        t={t}
        printerNameById={printerNameById}
      />,
    );
    expect(label('H2C-Beta')).toHaveTextContent('H2C-Beta');
  });

  it('names an unassigned row', () => {
    render(<QueueTargetChip item={item()} t={t} printerNameById={printerNameById} />);
    expect(label('queue.filter.unassigned')).toBeInTheDocument();
  });

  it('adds the location and filament suffixes to a MODEL pool', () => {
    render(
      <QueueTargetChip
        item={item({
          target_model: 'H2S',
          target_location: 'Bay 2',
          required_filament_types: ['PETG', 'PLA'],
        })}
        t={t}
        printerNameById={printerNameById}
      />,
    );
    expect(label('queue.filter.any H2S @ Bay 2 (PETG, PLA)')).toBeInTheDocument();
  });

  it('adds the same suffixes to a PRINTERS pool, naming every member', () => {
    render(
      <QueueTargetChip
        item={item({ target_printer_ids: [1, 2], target_location: 'Bay 2' })}
        t={t}
        printerNameById={printerNameById}
      />,
    );
    expect(label('queue.filter.anyOf H2S-Alpha, H2C-Beta @ Bay 2')).toBeInTheDocument();
  });

  it('gives a pinned row NO suffixes, even when the columns are set', () => {
    // The suffixes narrow a POOL. A landed row names the machine it is on.
    render(
      <QueueTargetChip
        item={item({
          printer_id: 1,
          printer_name: 'H2S-Alpha',
          target_location: 'Bay 2',
          required_filament_types: ['PETG'],
        })}
        t={t}
        printerNameById={printerNameById}
      />,
    );
    expect(label('H2S-Alpha')).toBeInTheDocument();
    expect(screen.queryByText(/Bay 2/)).not.toBeInTheDocument();
  });

  it('carries the full text in the tooltip, because the label truncates', () => {
    const full = 'queue.filter.anyOf H2S-Alpha, H2C-Beta @ Bay 2 (PETG)';
    render(
      <QueueTargetChip
        item={item({
          target_printer_ids: [1, 2],
          target_location: 'Bay 2',
          required_filament_types: ['PETG'],
        })}
        t={t}
        printerNameById={printerNameById}
      />,
    );
    const node = label(full);
    expect(node).toHaveClass('truncate');
    expect(node).toHaveTextContent(full);
  });
});

