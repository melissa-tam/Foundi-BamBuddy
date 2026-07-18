/**
 * Tests for the shared ``ui/Modal`` primitive: dialog semantics, the
 * hand-rolled focus trap (useFocusTrap), focus-on-open + restore-on-unmount,
 * and Escape / overlay dismissal gating.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Modal } from '../../../components/ui/Modal';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Modal — dialog semantics + accessible name', () => {
  it('renders role="dialog" with aria-modal and an aria-label name', () => {
    render(
      <Modal label="Delete item" onClose={vi.fn()}>
        <p>body</p>
      </Modal>,
    );
    const dialog = screen.getByRole('dialog', { name: 'Delete item' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('derives its accessible name from labelledBy (aria-labelledby)', () => {
    render(
      <Modal labelledBy="modal-heading" onClose={vi.fn()}>
        <h2 id="modal-heading">Confirm deletion</h2>
      </Modal>,
    );
    // getByRole resolves the name through aria-labelledby.
    expect(screen.getByRole('dialog', { name: 'Confirm deletion' })).toBeInTheDocument();
  });

  it('portals into document.body (not the render container)', () => {
    const { container } = render(
      <Modal label="Portaled" onClose={vi.fn()}>
        <p>body</p>
      </Modal>,
    );
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(document.body.querySelector('[role="dialog"]')).not.toBeNull();
  });
});

describe('Modal — focus trap (Tab / Shift+Tab cycling)', () => {
  it('Tab from the last focusable wraps to the first', async () => {
    const user = userEvent.setup();
    render(
      <Modal label="Trap" onClose={vi.fn()}>
        <button>First</button>
        <button>Second</button>
      </Modal>,
    );
    const first = screen.getByRole('button', { name: 'First' });
    const last = screen.getByRole('button', { name: 'Second' });

    last.focus();
    expect(last).toHaveFocus();
    await user.tab();
    expect(first).toHaveFocus();
  });

  it('Shift+Tab from the first focusable wraps to the last', async () => {
    const user = userEvent.setup();
    render(
      <Modal label="Trap" onClose={vi.fn()}>
        <button>First</button>
        <button>Second</button>
      </Modal>,
    );
    const first = screen.getByRole('button', { name: 'First' });
    const last = screen.getByRole('button', { name: 'Second' });

    first.focus();
    expect(first).toHaveFocus();
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
  });
});

describe('Modal — focus move on open + restore on unmount', () => {
  it('moves focus into the panel on open (first focusable)', () => {
    render(
      <Modal label="Focus" onClose={vi.fn()}>
        <button>Inside</button>
      </Modal>,
    );
    expect(screen.getByRole('button', { name: 'Inside' })).toHaveFocus();
  });

  it('restores focus to the trigger element on unmount', () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>Open</button>
          {open && (
            <Modal label="Restore" onClose={() => setOpen(false)}>
              <button onClick={() => setOpen(false)}>Close</button>
            </Modal>
          )}
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole('button', { name: 'Open' });
    trigger.focus();
    expect(trigger).toHaveFocus();

    // Open: focus captured from the trigger, then moved into the modal.
    fireEvent.click(trigger);
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();

    // Close (unmount): focus is restored to the trigger that opened it.
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(trigger).toHaveFocus();
  });
});

describe('Modal — Escape dismissal', () => {
  it('Escape calls onClose', () => {
    const onClose = vi.fn();
    render(
      <Modal label="Esc" onClose={onClose}>
        <p>body</p>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('Escape is suppressed when dismissDisabled', () => {
    const onClose = vi.fn();
    render(
      <Modal label="Esc" onClose={onClose} dismissDisabled>
        <p>body</p>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('Modal — overlay dismissal', () => {
  it('overlay click closes by default', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal label="Overlay" onClose={onClose}>
        <p>body</p>
      </Modal>,
    );
    const overlay = screen.getByRole('dialog').parentElement;
    expect(overlay).not.toBeNull();
    await user.click(overlay as HTMLElement);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('overlay click does nothing when closeOnOverlay is false', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal label="Overlay" onClose={onClose} closeOnOverlay={false}>
        <p>body</p>
      </Modal>,
    );
    const overlay = screen.getByRole('dialog').parentElement;
    await user.click(overlay as HTMLElement);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('clicking panel content does not close (stopPropagation)', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(
      <Modal label="Overlay" onClose={onClose}>
        <button>Inside</button>
      </Modal>,
    );
    await user.click(screen.getByRole('button', { name: 'Inside' }));
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('Modal — panel width', () => {
  it('applies the size-mapped max-width class by default', () => {
    render(
      <Modal label="Sized" size="lg" onClose={vi.fn()}>
        <p>body</p>
      </Modal>,
    );
    expect(screen.getByRole('dialog').className).toContain('max-w-2xl');
  });

  it('widthClass replaces the size mapping instead of stacking on it', () => {
    render(
      <Modal label="Wide" size="lg" widthClass="max-w-6xl" onClose={vi.fn()}>
        <p>body</p>
      </Modal>,
    );
    const cls = screen.getByRole('dialog').className;
    expect(cls).toContain('max-w-6xl');
    expect(cls).not.toContain('max-w-2xl');
  });
});
