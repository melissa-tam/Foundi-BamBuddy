/**
 * Tests for api.uploadLibraryFile — the XMLHttpRequest transport that powers
 * real upload-progress reporting in the library upload modal.
 *
 * The prior implementation used `fetch`; its characterized behavior (preserved
 * here as the parity baseline) was:
 *
 *   const response = await fetch(url, { method: 'POST', headers, body: formData });
 *   if (!response.ok) {
 *     const error = await response.json().catch(() => ({}));
 *     throw new Error(error.detail || `HTTP ${response.status}`);   // plain Error
 *   }
 *   return response.json();                                          // parsed JSON
 *
 * i.e. success resolves the parsed JSON body; an HTTP error rejects with a
 * PLAIN Error (not ApiError) whose message is the backend `detail` string, or
 * `HTTP <status>` when the body is not JSON. These tests assert the XHR path
 * reproduces exactly that, plus the new progress-callback wiring.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { api, setAuthToken } from '../../api/client';

interface ProgressLike {
  lengthComputable: boolean;
  loaded: number;
  total: number;
}

/** Minimal controllable XMLHttpRequest stand-in. Captures open/header/send and
 *  lets the test drive upload-progress, load, and error events by hand. */
class MockXHR {
  static instances: MockXHR[] = [];

  method = '';
  url = '';
  headers: Record<string, string> = {};
  body: unknown = null;
  status = 0;
  responseText = '';
  upload: { onprogress: ((event: ProgressLike) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor() {
    MockXHR.instances.push(this);
  }

  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }

  setRequestHeader(key: string, value: string) {
    this.headers[key] = value;
  }

  send(body: unknown) {
    this.body = body;
  }

  // --- test drivers ---
  emitProgress(loaded: number, total: number, lengthComputable = true) {
    this.upload.onprogress?.({ loaded, total, lengthComputable });
  }

  respond(status: number, responseText: string) {
    this.status = status;
    this.responseText = responseText;
    this.onload?.();
  }

  fail(status = 0) {
    this.status = status;
    this.onerror?.();
  }
}

const okBody = {
  id: 1,
  filename: 'model.gcode.3mf',
  file_type: '3mf',
  file_size: 1048576,
  thumbnail_path: null,
  duplicate_of: null,
  metadata: null,
};

beforeEach(() => {
  MockXHR.instances = [];
  vi.stubGlobal('XMLHttpRequest', MockXHR);
});

afterEach(() => {
  vi.unstubAllGlobals();
  setAuthToken(null);
});

const makeFile = () => new File(['content'], 'model.gcode.3mf', { type: 'application/octet-stream' });

describe('uploadLibraryFile — request wiring', () => {
  it('POSTs multipart form-data to the library files endpoint', () => {
    void api.uploadLibraryFile(makeFile(), null);
    const xhr = MockXHR.instances[0];
    expect(xhr.method).toBe('POST');
    expect(xhr.url).toBe('/api/v1/library/files?generate_stl_thumbnails=true');
    expect(xhr.body).toBeInstanceOf(FormData);
  });

  it('includes folder_id and generate_stl_thumbnails query params', () => {
    void api.uploadLibraryFile(makeFile(), 5, false);
    const xhr = MockXHR.instances[0];
    expect(xhr.url).toBe('/api/v1/library/files?folder_id=5&generate_stl_thumbnails=false');
  });

  it('attaches the Authorization header when a token is set', () => {
    setAuthToken('tok-123');
    void api.uploadLibraryFile(makeFile(), null);
    const xhr = MockXHR.instances[0];
    expect(xhr.headers['Authorization']).toBe('Bearer tok-123');
  });

  it('omits the Authorization header when no token is set', () => {
    void api.uploadLibraryFile(makeFile(), null);
    const xhr = MockXHR.instances[0];
    expect(xhr.headers['Authorization']).toBeUndefined();
  });
});

describe('uploadLibraryFile — progress callback', () => {
  it('reports computable progress events as integer percentages', async () => {
    const onProgress = vi.fn();
    const promise = api.uploadLibraryFile(makeFile(), null, true, { onProgress });
    const xhr = MockXHR.instances[0];

    xhr.emitProgress(0, 200);
    xhr.emitProgress(84, 200); // 42%
    xhr.emitProgress(200, 200); // 100%
    xhr.respond(200, JSON.stringify(okBody));

    await promise;
    expect(onProgress).toHaveBeenNthCalledWith(1, 0);
    expect(onProgress).toHaveBeenNthCalledWith(2, 42);
    expect(onProgress).toHaveBeenNthCalledWith(3, 100);
  });

  it('ignores non-computable progress events (no callback fired)', async () => {
    const onProgress = vi.fn();
    const promise = api.uploadLibraryFile(makeFile(), null, true, { onProgress });
    const xhr = MockXHR.instances[0];

    xhr.emitProgress(10, 0, false);
    xhr.respond(200, JSON.stringify(okBody));

    await promise;
    expect(onProgress).not.toHaveBeenCalled();
  });

  it('works without an onProgress callback', async () => {
    const promise = api.uploadLibraryFile(makeFile(), null);
    const xhr = MockXHR.instances[0];
    xhr.emitProgress(100, 100); // no upload.onprogress registered — must not throw
    xhr.respond(200, JSON.stringify(okBody));
    await expect(promise).resolves.toEqual(okBody);
  });
});

describe('uploadLibraryFile — success/error parity with the old fetch path', () => {
  it('resolves with the parsed JSON body on a 2xx', async () => {
    const promise = api.uploadLibraryFile(makeFile(), null);
    MockXHR.instances[0].respond(201, JSON.stringify(okBody));
    await expect(promise).resolves.toEqual(okBody);
  });

  it('rejects with a plain Error carrying the backend detail on an HTTP error', async () => {
    const promise = api.uploadLibraryFile(makeFile(), null);
    MockXHR.instances[0].respond(413, JSON.stringify({ detail: 'File too large' }));

    await expect(promise).rejects.toThrow('File too large');
    // Old path threw a plain `new Error(...)`, NOT an ApiError — assert the type.
    const err = await promise.catch((e: unknown) => e);
    expect(err).toBeInstanceOf(Error);
    expect((err as Error).name).toBe('Error');
    expect((err as { status?: number }).status).toBeUndefined();
  });

  it('falls back to `HTTP <status>` when the error body is not JSON', async () => {
    const promise = api.uploadLibraryFile(makeFile(), null);
    MockXHR.instances[0].respond(500, '<html>Internal Server Error</html>');
    await expect(promise).rejects.toThrow('HTTP 500');
  });

  it('falls back to `HTTP <status>` when the error JSON has no detail', async () => {
    const promise = api.uploadLibraryFile(makeFile(), null);
    MockXHR.instances[0].respond(422, JSON.stringify({ something: 'else' }));
    await expect(promise).rejects.toThrow('HTTP 422');
  });

  it('rejects on a network error', async () => {
    const promise = api.uploadLibraryFile(makeFile(), null);
    MockXHR.instances[0].fail(0);
    await expect(promise).rejects.toBeInstanceOf(Error);
  });
});
