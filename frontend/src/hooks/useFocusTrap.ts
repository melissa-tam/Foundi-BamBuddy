import { useEffect, type RefObject } from 'react';

/**
 * CSS selector matching the elements that participate in the Tab order.
 * Disabled controls and ``tabindex="-1"`` nodes are excluded so a programmatic
 * fallback focus target (e.g. the dialog panel itself) never traps the cycle.
 * Exported so the owning surface (``ui/Modal``) can reuse the exact same
 * definition when choosing an initial focus target — one source of truth.
 */
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

/**
 * Keep keyboard focus inside ``containerRef`` while ``active`` is true.
 *
 * Hand-rolled (no dependency): a ``keydown`` listener on the container
 * intercepts Tab / Shift+Tab and wraps focus from the last focusable element
 * back to the first (and vice-versa). Focus that has somehow escaped the
 * container is pulled back to the appropriate boundary. The listener lives on
 * the container itself — focus starts inside the trap, so the event always
 * bubbles up to it.
 */
export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  active: boolean,
): void {
  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    if (!container) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const focusable = Array.from(
        container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      );
      if (focusable.length === 0) {
        // Nothing tabbable inside — keep focus pinned to the container.
        e.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement;

      if (e.shiftKey) {
        if (activeEl === first || !container.contains(activeEl)) {
          e.preventDefault();
          last.focus();
        }
      } else if (activeEl === last || !container.contains(activeEl)) {
        e.preventDefault();
        first.focus();
      }
    };

    container.addEventListener('keydown', handleKeyDown);
    return () => container.removeEventListener('keydown', handleKeyDown);
  }, [active, containerRef]);
}
