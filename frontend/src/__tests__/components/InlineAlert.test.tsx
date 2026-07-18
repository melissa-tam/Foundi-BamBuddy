/**
 * Tests for the InlineAlert component — the shared in-flow status box used for
 * dialog-internal failures (extracted from the ProductionRunsPage duplicates).
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { InlineAlert } from '../../components/ui/InlineAlert';

describe('InlineAlert', () => {
  it('renders its children inside an alert region', () => {
    render(<InlineAlert severity="error">Something broke</InlineAlert>);

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Something broke');
  });

  describe('severity styles (paired light + dark shades)', () => {
    it('applies error styling', () => {
      render(<InlineAlert severity="error">err</InlineAlert>);

      const alert = screen.getByRole('alert');
      expect(alert.className).toContain('text-red-700');
      expect(alert.className).toContain('dark:text-red-300');
      expect(alert.className).toContain('border-red-500/40');
    });

    it('applies warning styling', () => {
      render(<InlineAlert severity="warning">warn</InlineAlert>);

      const alert = screen.getByRole('alert');
      expect(alert.className).toContain('text-yellow-700');
      expect(alert.className).toContain('dark:text-yellow-300');
    });

    it('applies info styling', () => {
      render(<InlineAlert severity="info">info</InlineAlert>);

      const alert = screen.getByRole('alert');
      expect(alert.className).toContain('text-blue-700');
      expect(alert.className).toContain('dark:text-blue-300');
    });
  });

  it('merges a custom className onto the container', () => {
    render(
      <InlineAlert severity="error" className="mt-4">
        x
      </InlineAlert>,
    );

    expect(screen.getByRole('alert').className).toContain('mt-4');
  });
});
