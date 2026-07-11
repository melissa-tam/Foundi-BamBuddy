/**
 * Tests for the HMSErrorSummary card line — the one-line, highest-severity
 * restatement of the printer's HMS faults, rendered under the badge row.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HMSErrorSummary } from '../../components/HMSErrorSummary';
import type { HMSError } from '../../api/client';

function mk(severity: number, description: string | null, short_code: string): HMSError {
  return {
    code: '0x0',
    attr: 0,
    module: 0,
    severity,
    description,
    short_code,
    wiki_url: 'https://wiki.bambulab.com/en/hms/home',
  };
}

describe('HMSErrorSummary', () => {
  afterEach(cleanup);

  it('renders nothing when there are no errors', () => {
    // The shared render wrapper mounts provider chrome (toast container), so
    // assert the summary itself produced no interactive element instead of an
    // empty container.
    render(<HMSErrorSummary errors={[]} onOpen={() => {}} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('surfaces the highest-severity error first', () => {
    const errors = [mk(4, 'Just info', '0300_C012'), mk(1, 'Fatal fault', '0300_4000')];
    render(<HMSErrorSummary errors={errors} onOpen={() => {}} />);
    expect(screen.getByText('Fatal fault')).toBeInTheDocument();
    expect(screen.queryByText('Just info')).not.toBeInTheDocument();
  });

  it('uses role="alert" on error tone (a fatal/serious code is present)', () => {
    render(<HMSErrorSummary errors={[mk(1, 'Fatal fault', '0300_4000')]} onOpen={() => {}} />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
  });

  it('does NOT use role="alert" for a warning-only tone', () => {
    render(<HMSErrorSummary errors={[mk(3, 'A warning', '0300_8061')]} onOpen={() => {}} />);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button')).toBeInTheDocument();
  });

  it('exposes the full text via the title attribute (truncation tooltip)', () => {
    const long = 'A very long fault description that would otherwise be truncated on the printer card';
    render(<HMSErrorSummary errors={[mk(3, long, '0300_8061')]} onOpen={() => {}} />);
    expect(screen.getByText(long).closest('button')).toHaveAttribute('title', long);
  });

  it('falls back to the short code when the description is unknown (null)', () => {
    render(<HMSErrorSummary errors={[mk(3, null, 'FFFF_FFFF')]} onOpen={() => {}} />);
    expect(screen.getByText('FFFF_FFFF')).toBeInTheDocument();
  });

  it('calls onOpen when clicked', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<HMSErrorSummary errors={[mk(3, 'A warning', '0300_8061')]} onOpen={onOpen} />);
    await user.click(screen.getByText('A warning'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});
