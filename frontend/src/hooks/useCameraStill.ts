/**
 * THE `['camera-still', printerId]` query: a touch tile's camera picture, read from the
 * server's still owner (`GET /printers/{id}/camera/still`, `services/camera_stills`).
 *
 * The tile only ASKS — every `CAMERA_STILL_POLL_MS` — and the server decides whether a
 * capture is due (1 -> 5 -> 15 min while nothing about the printer changes, back to
 * 1 min on a change), so the captures a printer sees never grow with the browsers
 * polling it. The held frame's ETag rides each ask; a 304 keeps the frame held.
 *
 * The frame is kept as a data URL (no object URL to revoke, nothing for StrictMode's
 * double effects to tear down).
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';

/** How often a tile asks for its still; the server owns the capture cadence. */
export const CAMERA_STILL_POLL_MS = 60_000;

export interface HeldStill {
  /** `data:image/jpeg;base64,…` */
  src: string;
  /** ISO-8601 UTC: when the frame was taken; null when the server did not say. */
  capturedAt: string | null;
  etag: string | null;
}

export const cameraStillKey = (printerId: number) => ['camera-still', printerId] as const;

async function toDataUrl(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return `data:${blob.type || 'image/jpeg'};base64,${btoa(binary)}`;
}

/**
 * The tile's still: `HeldStill`, or null when the server has none (camera off, capture
 * failed). `enabled` is the tile's own verdict (`utils/touchOrder.stillWanted`).
 */
export function useCameraStill(printerId: number, enabled: boolean) {
  const queryClient = useQueryClient();
  return useQuery({
    queryKey: cameraStillKey(printerId),
    queryFn: async (): Promise<HeldStill | null> => {
      const held = queryClient.getQueryData<HeldStill | null>(cameraStillKey(printerId)) ?? null;
      const answer = await api.getCameraStill(printerId, held?.etag ?? null);
      if (answer.kind === 'unchanged') return held;
      if (answer.kind === 'none') return null;
      return { src: await toDataUrl(answer.blob), capturedAt: answer.capturedAt, etag: answer.etag };
    },
    enabled,
    refetchInterval: CAMERA_STILL_POLL_MS,
    staleTime: CAMERA_STILL_POLL_MS,
  });
}
