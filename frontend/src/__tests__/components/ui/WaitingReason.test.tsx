/**
 * WaitingReason (F3): the shared inline "why is this held" line. Verifies it
 * renders translated copy for a mapped token, keeps the raw machine token in
 * `title` for inspection, humanizes an unmapped token, and shows a
 * backend-authored sentence verbatim with NO title.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import { render } from '../../utils';
import { WaitingReason } from '../../../components/ui/WaitingReason';

describe('WaitingReason', () => {
  afterEach(cleanup);

  it('renders translated copy for a mapped token, raw token in title', () => {
    render(<WaitingReason reason="printer_offline_stalled" />);
    const line = screen.getByText(/offline mid-print/i).closest('p');
    expect(line).not.toBeNull();
    expect(line).toHaveAttribute('title', 'printer_offline_stalled');
  });

  it('humanizes an unmapped token and keeps the raw token in title', () => {
    render(<WaitingReason reason="some_new_hold" />);
    const line = screen.getByText('Some new hold').closest('p');
    expect(line).toHaveAttribute('title', 'some_new_hold');
  });

  it('renders a backend-authored sentence verbatim with no title', () => {
    render(<WaitingReason reason="Nozzle 0.4 != required 0.6" />);
    const line = screen.getByText('Nozzle 0.4 != required 0.6').closest('p');
    expect(line).not.toBeNull();
    expect(line).not.toHaveAttribute('title');
  });

  it('applies the caller className alongside its own layout classes', () => {
    render(<WaitingReason reason="stagger_hold" className="mt-2 text-xs" />);
    const line = screen.getByText(/power-stagger window/i).closest('p');
    expect(line).toHaveClass('mt-2', 'text-xs', 'flex');
  });
});
