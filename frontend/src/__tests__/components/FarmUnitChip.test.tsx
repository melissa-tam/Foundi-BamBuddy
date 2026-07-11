/**
 * FarmUnitChip (Phase 3, F2): the printer-card chip that explains why a printer
 * is on (or blocked on) farm work — a run link plus ONE status line per state
 * (printing / staged / low-spool / waiting reason / last-unit failure), and its
 * absence when the printer holds no farm context.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { FarmUnitChip } from '../../components/FarmUnitChip';
import type { FarmPrinterContext } from '../../api/client';

function mk(overrides: Partial<FarmPrinterContext> = {}): FarmPrinterContext {
  return {
    printer_id: 1,
    run_id: 7,
    run_name: 'WID-001 run',
    sku_code: 'WID-001',
    run_status: 'active',
    pause_reason: null,
    unit_id: 123,
    unit_status: 'printing',
    waiting_reason: null,
    error_message: null,
    staged: false,
    filament_short: false,
    first_article: false,
    first_article_state: null,
    ...overrides,
  };
}

describe('FarmUnitChip', () => {
  afterEach(cleanup);

  it('renders nothing when there is no context', () => {
    render(<FarmUnitChip ctx={null} />);
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('links to the run detail and shows the printing unit', () => {
    render(<FarmUnitChip ctx={mk({ unit_status: 'printing', unit_id: 123 })} />);
    const link = screen.getByRole('link', { name: /Run: WID-001 run/ });
    expect(link).toHaveAttribute('href', '/production-runs/7');
    expect(screen.getByText('Printing unit #123')).toBeInTheDocument();
  });

  it('shows the plain staged line for an operator-staged unit', () => {
    render(<FarmUnitChip ctx={mk({ unit_status: 'pending', unit_id: 5, staged: true })} />);
    expect(screen.getByText(/Unit staged — resume the run to release/)).toBeInTheDocument();
  });

  it('shows the low-spool variant when a staged unit is filament-short', () => {
    render(
      <FarmUnitChip
        ctx={mk({ unit_status: 'pending', unit_id: 5, staged: true, filament_short: true })}
      />,
    );
    expect(screen.getByText(/filament low — swap spool or release from Queue/)).toBeInTheDocument();
    // Not the plain staged copy.
    expect(screen.queryByText(/resume the run to release/)).toBeNull();
  });

  it('surfaces a waiting-reason machine code over the printing line', () => {
    render(
      <FarmUnitChip
        ctx={mk({ unit_status: 'printing', waiting_reason: 'printer_offline_stalled' })}
      />,
    );
    expect(screen.getByText(/offline mid-print/i)).toBeInTheDocument();
    expect(screen.queryByText(/Printing unit/)).toBeNull();
  });

  it('shows the last failure truncated with the full text in a title', () => {
    const error = 'HMS 0300_8017 heatbed foreign object detected before print start';
    render(
      <FarmUnitChip
        ctx={mk({ unit_status: 'failed', unit_id: 9, error_message: error })}
      />,
    );
    const line = screen.getByText(`Last unit failed: ${error}`);
    expect(line).toHaveAttribute('title', error);
    expect(line).toHaveClass('truncate');
  });

  it('stops click propagation so the card handlers do not fire', async () => {
    const parentClick = vi.fn();
    render(
      <div onClick={parentClick}>
        <FarmUnitChip ctx={mk()} />
      </div>,
    );
    await userEvent.click(screen.getByRole('link', { name: /Run: WID-001 run/ }));
    expect(parentClick).not.toHaveBeenCalled();
  });
});
