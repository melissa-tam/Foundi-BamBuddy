/**
 * Tests for OutOfRotationChip — the "Return to rotation" control that clears a
 * spool's feed-fault flag (#feed-fault, 3a).
 *
 * Behaviour under test:
 *  - renders the status label + the action button,
 *  - clicking fires PATCH /inventory/spools/{id} with an EXPLICIT null
 *    feed_fault_at in the body (the backend's manual-clear signal),
 *  - success shows a toast AND invalidates the spool/assignment queries,
 *  - failure shows an error toast.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { createTestQueryClient } from '../utils';
import { ToastProvider } from '../../contexts/ToastContext';
import { OutOfRotationChip } from '../../components/OutOfRotationChip';

function renderChip(ui: React.ReactElement, queryClient = createTestQueryClient()) {
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ToastProvider>{ui}</ToastProvider>
    </QueryClientProvider>,
  );
  return { queryClient, ...result };
}

describe('OutOfRotationChip', () => {
  it('renders the status label and the return-to-rotation action', () => {
    renderChip(<OutOfRotationChip spoolId={5} faultCode="0700_8010" />);
    expect(screen.getByText('Out of rotation')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /return to rotation/i })).toBeInTheDocument();
  });

  it('puts the long tooltip copy (plus fault code) on the status title', () => {
    renderChip(<OutOfRotationChip spoolId={5} faultCode="0700_8010" />);
    const status = screen.getByText('Out of rotation');
    expect(status.getAttribute('title')).toContain('spool jam detected');
    expect(status.getAttribute('title')).toContain('0700_8010');
  });

  it('clicking fires PATCH with an explicit null feed_fault_at and invalidates queries', async () => {
    let capturedBody: Record<string, unknown> | null = null;
    let hadFeedFaultKey = false;
    let patchedId: string | undefined;
    server.use(
      http.patch('/api/v1/inventory/spools/:id', async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        capturedBody = body;
        hadFeedFaultKey = 'feed_fault_at' in body;
        patchedId = String(params.id);
        return HttpResponse.json({ id: Number(params.id), feed_fault_at: null, feed_fault_code: null });
      }),
    );

    const { queryClient } = renderChip(<OutOfRotationChip spoolId={42} faultCode="0700_8010" />);
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    await userEvent.click(screen.getByRole('button', { name: /return to rotation/i }));

    await waitFor(() => expect(capturedBody).not.toBeNull());
    // Explicit null is present in the body — not merely absent/undefined.
    expect(hadFeedFaultKey).toBe(true);
    expect(capturedBody).toEqual({ feed_fault_at: null });
    expect((capturedBody as Record<string, unknown>).feed_fault_at).toBeNull();
    expect(patchedId).toBe('42');

    // Success toast + query invalidation for the spool + printer surfaces.
    expect(await screen.findByText('Spool returned to rotation')).toBeInTheDocument();
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['inventory-spools'] }),
    );
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['spool-assignments'] });
  });

  it('shows an error toast when the clear request fails', async () => {
    server.use(
      http.patch('/api/v1/inventory/spools/:id', () =>
        HttpResponse.json({ detail: 'boom' }, { status: 500 }),
      ),
    );

    renderChip(<OutOfRotationChip spoolId={7} />);
    await userEvent.click(screen.getByRole('button', { name: /return to rotation/i }));

    expect(await screen.findByText('Could not return spool to rotation')).toBeInTheDocument();
  });
});
