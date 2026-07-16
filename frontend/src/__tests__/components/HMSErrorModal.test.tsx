/**
 * Tests for the HMSErrorModal component.
 *
 * Post-Phase-2: the modal renders EVERY error (descriptions, short code, and
 * wiki link ride the API payload). Unknown/novel codes are no longer hidden —
 * they render with a translated fallback so a lights-out farm never shows a
 * faulting printer as "OK".
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { HMSErrorModal } from '../../components/HMSErrorModal';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { HMSError } from '../../api/client';

const WIKI = 'https://wiki.bambulab.com/en/hms/home';

// Error code 0300_400C = "The task was canceled." (known code — backend
// resolves the description and short_code and hands them to the client).
const knownError: HMSError = {
  attr: 0x0300,
  code: '0x400C',
  severity: 2,
  // 16-hex hms[]-array full code — the modal renders it as four hyphen-groups,
  // not the lossy two-group short code.
  full_code: '0500010000030004',
  short_code: '0500_0004',
  description: 'The task was canceled.',
  wiki_url: WIKI,
};

// Error code FFFF_FFFF = unknown — backend resolves no description (null) but
// STILL sends the short code and wiki link so the fault stays visible.
const unknownError: HMSError = {
  attr: 0xFFFF,
  code: '0xFFFF',
  severity: 1,
  short_code: 'FFFF_FFFF',
  description: null,
  wiki_url: WIKI,
};

describe('HMSErrorModal', () => {
  const defaultProps = {
    printerName: 'Test Printer',
    errors: [knownError],
    onClose: vi.fn(),
    printerId: 1,
    hasPermission: vi.fn().mockReturnValue(true) as unknown as (permission: 'printers:control') => boolean,
  };

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  describe('rendering', () => {
    it('renders the modal title with printer name', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('Errors - Test Printer')).toBeInTheDocument();
    });

    it('shows error description for known error codes', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('The task was canceled.')).toBeInTheDocument();
    });

    it('renders the full 16-hex code as four hyphen-groups (not the lossy short code)', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('[0500-0100-0003-0004]')).toBeInTheDocument();
    });

    it('renders the severity label via i18n', () => {
      render(<HMSErrorModal {...defaultProps} />);
      // severity 2 => hmsErrors.severity.serious
      expect(screen.getByText('Serious')).toBeInTheDocument();
    });

    it('renders unknown codes with the fallback description AND their short code', () => {
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      // The fallback description is shown (i18n), not hidden.
      expect(screen.getByText(/Unknown printer error/)).toBeInTheDocument();
      // The short code is still visible (rendered as MMMM-CCCC).
      expect(screen.getByText('[FFFF-FFFF]')).toBeInTheDocument();
      // "No errors" is NOT shown — the error is rendered.
      expect(screen.queryByText('No errors')).not.toBeInTheDocument();
    });

    it('shows no errors message when errors array is empty', () => {
      render(<HMSErrorModal {...defaultProps} errors={[]} />);
      expect(screen.getByText('No errors')).toBeInTheDocument();
    });
  });

  describe('clear errors button', () => {
    it('shows clear button when there are errors', () => {
      render(<HMSErrorModal {...defaultProps} />);
      expect(screen.getByText('Clear Errors')).toBeInTheDocument();
    });

    it('hides clear button when there are no errors', () => {
      render(<HMSErrorModal {...defaultProps} errors={[]} />);
      expect(screen.queryByText('Clear Errors')).not.toBeInTheDocument();
    });

    it('shows clear button when errors are unknown codes (still clearable)', () => {
      render(<HMSErrorModal {...defaultProps} errors={[unknownError]} />);
      expect(screen.getByText('Clear Errors')).toBeInTheDocument();
    });

    it('disables clear button when user lacks permission', () => {
      const noPermission = vi.fn().mockReturnValue(false) as unknown as (permission: 'printers:control') => boolean;
      render(<HMSErrorModal {...defaultProps} hasPermission={noPermission} />);
      expect(screen.getByText('Clear Errors').closest('button')).toBeDisabled();
    });

    it('calls API and closes modal on successful clear', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();

      server.use(
        http.post('/api/v1/printers/1/hms/clear', () => {
          return HttpResponse.json({ success: true, message: 'HMS errors cleared' });
        })
      );

      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      await user.click(screen.getByText('Clear Errors'));

      await waitFor(() => {
        expect(onClose).toHaveBeenCalledTimes(1);
      });
    });

    it('shows error toast on failed clear', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();

      server.use(
        http.post('/api/v1/printers/1/hms/clear', () => {
          return HttpResponse.json({ detail: 'Failed' }, { status: 500 });
        })
      );

      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      await user.click(screen.getByText('Clear Errors'));

      await waitFor(() => {
        expect(onClose).not.toHaveBeenCalled();
      });
    });
  });

  describe('interactions', () => {
    it('calls onClose when X button is clicked', async () => {
      const user = userEvent.setup();
      const onClose = vi.fn();
      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      // The X button is the button with the X icon in the header
      const closeButtons = screen.getAllByRole('button');
      // First button is the X close button in the header
      await user.click(closeButtons[0]);
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('calls onClose when Escape key is pressed', () => {
      const onClose = vi.fn();
      render(<HMSErrorModal {...defaultProps} onClose={onClose} />);

      fireEvent.keyDown(window, { key: 'Escape' });
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });
});
