import { useEffect, useRef, type ReactNode, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { Card } from '../Card';
import { FOCUSABLE_SELECTOR, useFocusTrap } from '../../hooks/useFocusTrap';

const SIZE_CLASSES = {
  sm: 'max-w-md',
  md: 'max-w-lg',
  lg: 'max-w-2xl',
  xl: 'max-w-4xl',
} as const;

export type ModalSize = keyof typeof SIZE_CLASSES;

/**
 * Accessible-name contract: exactly one of ``label`` (rendered as
 * ``aria-label``) or ``labelledBy`` (an element id, rendered as
 * ``aria-labelledby``) MUST be supplied. The union makes "neither" and "both"
 * unrepresentable at the type level, so every Modal is guaranteed a name for
 * assistive tech.
 */
type ModalNameProps =
  | { label: string; labelledBy?: never }
  | { label?: never; labelledBy: string };

interface ModalBaseProps {
  /** Panel body. Wrapped in a themed ``Card`` — pass ``CardContent`` etc. */
  children: ReactNode;
  /** Invoked on Escape or overlay click (both gated by ``dismissDisabled``). */
  onClose: () => void;
  /** Panel max-width. Defaults to ``md`` (``max-w-lg``). */
  size?: ModalSize;
  /**
   * Explicit width utility class that REPLACES the ``size`` mapping (e.g.
   * ``max-w-3xl``, ``w-[560px]``). Use when the panel needs a width outside
   * the size scale — never stack a competing ``max-w-*`` via ``className``,
   * where stylesheet order (not class order) would decide the winner.
   */
  widthClass?: string;
  /**
   * Element focused when the modal opens. Falls back to the first focusable
   * element inside the panel, then the panel itself.
   */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /**
   * Block Escape AND overlay-click dismissal (e.g. while a mutation is in
   * flight). Does not affect explicit close buttons rendered in ``children``.
   */
  dismissDisabled?: boolean;
  /** Whether a click on the backdrop closes the modal. Defaults to ``true``. */
  closeOnOverlay?: boolean;
  /**
   * Tailwind z-index utility for the fixed overlay. Defaults to ``z-50``. Pass
   * a higher stack (e.g. ``z-[110]``) when opening from inside another modal
   * so this one sits in front (#1336 stacking contract).
   */
  overlayZIndex?: string;
  /** Extra classes merged onto the panel ``Card``. */
  className?: string;
}

type ModalProps = ModalBaseProps & ModalNameProps;

/**
 * Shared, accessible dialog primitive for farm-owned UI.
 *
 * Portals to ``document.body``; renders ``role="dialog" aria-modal="true"``
 * with a required accessible name; traps Tab focus; moves focus into the panel
 * on open and restores it to the previously focused element on unmount. Escape
 * and backdrop clicks close (unless ``dismissDisabled``). No body scroll-lock
 * (deliberate parity with the app's existing hand-rolled modals).
 */
export function Modal({
  children,
  onClose,
  size = 'md',
  widthClass,
  initialFocusRef,
  dismissDisabled = false,
  closeOnOverlay = true,
  overlayZIndex = 'z-50',
  className = '',
  label,
  labelledBy,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useFocusTrap(panelRef, true);

  // Capture the element focused before the modal opened, and restore focus to
  // it on unmount. Declared first so the capture runs before focus is moved
  // into the panel below, and its cleanup runs last on unmount.
  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    return () => {
      previouslyFocused.current?.focus?.();
    };
  }, []);

  // Move focus into the panel when it opens.
  useEffect(() => {
    const target =
      initialFocusRef?.current ??
      panelRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ??
      panelRef.current;
    target?.focus();
  }, [initialFocusRef]);

  // Escape closes (unless dismissal is blocked).
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !dismissDisabled) onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose, dismissDisabled]);

  const handleOverlayClick = () => {
    if (!dismissDisabled && closeOnOverlay) onClose();
  };

  return createPortal(
    <div
      className={`fixed inset-0 bg-black/70 flex items-center justify-center p-4 ${overlayZIndex}`}
      onClick={handleOverlayClick}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        aria-labelledby={labelledBy}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className={`w-full ${widthClass ?? SIZE_CLASSES[size]} focus:outline-none`}
      >
        <Card className={`max-h-[90vh] overflow-y-auto ${className}`}>{children}</Card>
      </div>
    </div>,
    document.body,
  );
}
