/**
 * Tests for QueuePhaseChip — the live dispatch-phase chip rendered on a queue
 * row (Phase C). It subscribes to the WS-populated ['queueItemPhase', itemId]
 * cache; here we seed that cache directly and assert the rendered chip.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createTestQueryClient } from '../utils';
import { QueuePhaseChip } from '../../components/QueuePhaseChip';
import type { QueueItemPhaseState } from '../../api/client';

function renderChip(itemId: number, phase: QueueItemPhaseState | null) {
  const queryClient = createTestQueryClient();
  if (phase) queryClient.setQueryData(['queueItemPhase', itemId], phase);
  return render(
    <QueryClientProvider client={queryClient}>
      <QueuePhaseChip itemId={itemId} />
    </QueryClientProvider>,
  );
}

describe('QueuePhaseChip', () => {
  it('renders the uploading chip with the rounded percentage', () => {
    renderChip(1, { phase: 'uploading', progress_pct: 62.4, status: 'printing', ts: 't' });
    const chip = screen.getByText('Uploading 62 %');
    expect(chip).toBeInTheDocument();
    // Not colour-only: the progress is in the accessible label too.
    expect(chip.closest('[aria-label]')?.getAttribute('aria-label')).toBe('Uploading 62 %');
  });

  it('falls back to a pct-less label when progress is null', () => {
    renderChip(2, { phase: 'uploading', progress_pct: null, status: 'printing', ts: 't' });
    expect(screen.getByText('Uploading…')).toBeInTheDocument();
  });

  it('renders the failed chip', () => {
    renderChip(3, { phase: 'failed', progress_pct: null, status: 'failed', ts: 't' });
    expect(screen.getByText('Failed')).toBeInTheDocument();
  });

  it('suppresses the chip once the print is actually running (phase printing)', () => {
    const { container } = renderChip(4, {
      phase: 'printing',
      progress_pct: null,
      status: 'printing',
      ts: 't',
    });
    expect(container.textContent).toBe('');
  });

  it('renders nothing when there is no live phase', () => {
    const { container } = renderChip(5, null);
    expect(container.textContent).toBe('');
  });
});
