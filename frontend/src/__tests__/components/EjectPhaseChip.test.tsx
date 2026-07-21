/**
 * Tests for EjectPhaseChip — the live eject-phase chip rendered on a printer
 * card (Phase C). It subscribes to the WS-populated ['ejectProgress', printerId]
 * cache; here we seed that cache directly and assert the rendered chip.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createTestQueryClient } from '../utils';
import { EjectPhaseChip } from '../../components/EjectPhaseChip';
import type { EjectPhaseState } from '../../api/client';

function renderChip(printerId: number, state: EjectPhaseState | null) {
  const queryClient = createTestQueryClient();
  if (state) queryClient.setQueryData(['ejectProgress', printerId], state);
  return render(
    <QueryClientProvider client={queryClient}>
      <EjectPhaseChip printerId={printerId} />
    </QueryClientProvider>,
  );
}

describe('EjectPhaseChip', () => {
  it('renders the uploading chip with the rounded percentage', () => {
    renderChip(7, { phase: 'uploading', progress_pct: 62.6, queue_item_id: 12, ts: 't' });
    expect(screen.getByText('Ejecting: uploading 63 %')).toBeInTheDocument();
  });

  it('renders the sweeping chip', () => {
    renderChip(7, { phase: 'sweeping', progress_pct: null, queue_item_id: null, ts: 't' });
    expect(screen.getByText('Ejecting: sweeping…')).toBeInTheDocument();
  });

  it('renders the failed chip', () => {
    renderChip(7, { phase: 'failed', progress_pct: null, queue_item_id: null, ts: 't' });
    expect(screen.getByText('Eject failed')).toBeInTheDocument();
  });

  it('renders nothing when there is no live eject', () => {
    const { container } = renderChip(7, null);
    expect(container.textContent).toBe('');
  });
});
