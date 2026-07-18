/**
 * Progress-percentage rendering for FileUploadModal.
 *
 * The upload row shows the live percentage (e.g. "42%") once XHR reports a
 * computable upload-progress event, and falls back to the spinner while the
 * percentage is still unknown. These tests spy on `api.uploadLibraryFile` so
 * the progress callback and pending/settled state are fully controllable
 * (kept in a separate file from the MSW-driven FileUploadModal suite).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { FileUploadModal } from '../../components/FileUploadModal';
import { api } from '../../api/client';
import type { LibraryFileUploadResponse } from '../../api/client';

const uploadResponse: LibraryFileUploadResponse = {
  id: 1,
  filename: 'model.gcode.3mf',
  file_type: '3mf',
  file_size: 1024,
  thumbnail_path: null,
  duplicate_of: null,
  metadata: null,
};

const defaultProps = {
  folderId: null as number | null,
  onClose: vi.fn(),
  onUploadComplete: vi.fn(),
};

async function addFileAndUpload() {
  const user = userEvent.setup();
  render(<FileUploadModal {...defaultProps} />);
  const file = new File(['content'], 'model.gcode.3mf', { type: 'application/octet-stream' });
  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
  await user.upload(fileInput, file);
  await user.click(screen.getByRole('button', { name: /Upload \(1\)/i }));
}

describe('FileUploadModal upload progress', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the live percentage while uploading once progress is reported', async () => {
    // Report 42% then keep the upload pending so the row stays in "uploading".
    let resolveUpload: (value: LibraryFileUploadResponse) => void = () => {};
    vi.spyOn(api, 'uploadLibraryFile').mockImplementation((_file, _folderId, _thumbs, opts) => {
      opts?.onProgress?.(42);
      return new Promise<LibraryFileUploadResponse>((resolve) => {
        resolveUpload = resolve;
      });
    });

    await addFileAndUpload();

    const progressbar = await screen.findByRole('progressbar');
    expect(progressbar).toHaveTextContent('42%');
    expect(progressbar).toHaveAttribute('aria-valuenow', '42');

    resolveUpload(uploadResponse);
    await waitFor(() => expect(defaultProps.onUploadComplete).toHaveBeenCalled());
  });

  it('shows the spinner (no percentage) while progress is still unknown', async () => {
    // No onProgress call — the row must keep the spinner, not a progressbar.
    let resolveUpload: (value: LibraryFileUploadResponse) => void = () => {};
    vi.spyOn(api, 'uploadLibraryFile').mockImplementation(() => {
      return new Promise<LibraryFileUploadResponse>((resolve) => {
        resolveUpload = resolve;
      });
    });

    await addFileAndUpload();

    // The button shows "Uploading..." — wait for that, then assert the row has
    // no progressbar (percentage) yet.
    await screen.findByText('Uploading...');
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();

    resolveUpload(uploadResponse);
    await waitFor(() => expect(defaultProps.onUploadComplete).toHaveBeenCalled());
  });
});
