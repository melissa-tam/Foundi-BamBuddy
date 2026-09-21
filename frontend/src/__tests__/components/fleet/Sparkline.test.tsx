/**
 * Sparkline — the trend column's shape.
 *
 * The interesting behaviour is all about ABSENCE: a bucket with no figure must
 * break the line rather than be interpolated across, a window with one observed
 * bucket must still draw something, and a window with nothing observed must
 * draw nothing rather than a flat line at zero. Those are the three ways a
 * sparkline can quietly invent data.
 *
 * Geometry is read off the SVG elements directly because an `<svg>` child has
 * no role of its own — the accessible surface is the `role="img"` wrapper,
 * which every test goes through first.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Sparkline } from '../../../components/fleet/Sparkline';

const nameOf = (label: string) => screen.getByRole('img', { name: label });

describe('Sparkline', () => {
  it('exposes the caller-supplied accessible name', () => {
    render(<Sparkline points={[1, 2, 3]} ariaLabel="Trend" />);

    expect(nameOf('Trend')).toBeInTheDocument();
  });

  it('draws one unbroken line when every bucket has a figure', () => {
    render(<Sparkline points={[1, 2, 3, 4]} ariaLabel="Trend" />);

    const svg = nameOf('Trend');
    expect(svg.querySelectorAll('polyline')).toHaveLength(1);
    expect(svg.querySelectorAll('circle')).toHaveLength(0);
  });

  it('breaks the line at a bucket with no figure instead of interpolating', () => {
    render(<Sparkline points={[1, 2, null, 4, 5]} ariaLabel="Trend" />);

    const svg = nameOf('Trend');
    // Two runs, two polylines — never one line drawn straight over the gap.
    expect(svg.querySelectorAll('polyline')).toHaveLength(2);
  });

  it('renders a lone observed bucket as a dot, because a one-point line draws nothing', () => {
    render(<Sparkline points={[null, null, 7, null]} ariaLabel="Trend" />);

    const svg = nameOf('Trend');
    expect(svg.querySelectorAll('circle')).toHaveLength(1);
    expect(svg.querySelectorAll('polyline')).toHaveLength(0);
  });

  it('renders nothing at all when no bucket has a figure', () => {
    render(<Sparkline points={[null, null, null]} ariaLabel="Trend" />);

    expect(screen.queryByRole('img', { name: 'Trend' })).not.toBeInTheDocument();
  });

  it('renders nothing for an empty series', () => {
    render(<Sparkline points={[]} ariaLabel="Trend" />);

    expect(screen.queryByRole('img', { name: 'Trend' })).not.toBeInTheDocument();
  });

  it('keeps a flat series on the mid-line rather than pinning it to an edge', () => {
    render(<Sparkline points={[5, 5, 5]} ariaLabel="Trend" height={20} />);

    const polyline = nameOf('Trend').querySelector('polyline');
    const ys = (polyline?.getAttribute('points') ?? '')
      .split(' ')
      .map((pair) => Number(pair.split(',')[1]));
    expect(ys).toHaveLength(3);
    // Mid-line of a 20px box with 2px padding on each side.
    ys.forEach((y) => expect(y).toBeCloseTo(10, 5));
  });
});
