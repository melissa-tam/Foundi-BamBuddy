/**
 * The tile's still: asked only when enabled, the held frame's ETag rides the next ask, a
 * 304 keeps the frame, a 503 means no picture.
 */
import { describe, expect, it } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { http, HttpResponse } from 'msw';
import type { ReactNode } from 'react';
import { server } from '../mocks/server';
import { createTestQueryClient } from '../utils';
import { useCameraStill } from '../../hooks/useCameraStill';

function wrapper() {
  const client = createTestQueryClient();
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, Wrapper };
}

describe('useCameraStill', () => {
  it('never asks while disabled', async () => {
    let asked = 0;
    server.use(http.get('/api/v1/printers/:id/camera/still', () => {
      asked += 1;
      return new HttpResponse(null, { status: 503 });
    }));
    const { Wrapper } = wrapper();
    const { result } = renderHook(() => useCameraStill(1, false), { wrapper: Wrapper });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked).toBe(0);
    expect(result.current.data).toBeUndefined();
  });

  it('holds the frame across a 304 and sends its ETag', async () => {
    const seen: (string | null)[] = [];
    server.use(http.get('/api/v1/printers/:id/camera/still', ({ request }) => {
      const etag = request.headers.get('if-none-match');
      seen.push(etag);
      if (etag === '"1-5"') return new HttpResponse(null, { status: 304 });
      return new HttpResponse(new Uint8Array([1, 2, 3]), {
        headers: { 'Content-Type': 'image/jpeg', ETag: '"1-5"', 'X-Captured-At': '2026-10-16T10:00:00+00:00' },
      });
    }));
    const { Wrapper, client } = wrapper();
    const { result } = renderHook(() => useCameraStill(1, true), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.data?.src).toBe('data:image/jpeg;base64,AQID'));
    expect(result.current.data?.capturedAt).toBe('2026-10-16T10:00:00+00:00');

    await client.refetchQueries({ queryKey: ['camera-still', 1] });
    expect(seen).toEqual([null, '"1-5"']);
    expect(result.current.data?.src).toBe('data:image/jpeg;base64,AQID');
  });

  it('reads a 503 as no picture', async () => {
    server.use(http.get('/api/v1/printers/:id/camera/still', () => new HttpResponse(null, { status: 503 })));
    const { Wrapper } = wrapper();
    const { result } = renderHook(() => useCameraStill(1, true), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
  });
});
