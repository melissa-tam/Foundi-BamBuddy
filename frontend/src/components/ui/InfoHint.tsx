/**
 * InfoHint — a small, plain-English help affordance rendered beside a field
 * label (an `Info` icon that reveals a short tooltip).
 *
 * Promoted from the local copy that lived in `EjectProfilesPage` (plan item
 * F7). The original was a hover-only native `title=` on a non-focusable
 * `<span role="img">`: invisible to keyboard and touch users. This shared
 * primitive keeps the identical look and the mouse-hover behaviour, and adds
 * the missing affordances:
 *
 * - The trigger is a real `<button type="button">`, so it is in the tab order
 *   and keyboard-operable, with its `text` as the accessible name.
 * - The tooltip panel appears on hover, on keyboard focus, and on a pointer
 *   tap (touch), and hides on unhover, blur, Escape, or a press outside.
 * - While visible the panel carries `role="tooltip"` and is linked to the
 *   trigger via `aria-describedby` (WCAG 2.2 SC 1.4.13 — dismissable via
 *   Escape, and hoverable because the pointer can move from the trigger onto
 *   the panel without it closing).
 *
 * Visibility is derived from three independent sources — pointer hover,
 * keyboard focus, and a touch tap-toggle — rather than synced into one flag by
 * an effect, so the mouse-hover and touch-tap paths never fight each other.
 * Positioning is plain CSS (absolutely positioned below the trigger); no
 * tooltip/positioning dependency is pulled in. Colours are the theme-aware
 * farm tokens, so it reads correctly in both light and dark themes. Kept
 * OUTSIDE any `<label>` element by callers so the field's own accessible name
 * stays exactly the visible label text.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { Info } from 'lucide-react';

interface InfoHintProps {
  /** Plain-English help text shown in the tooltip and used as the trigger's
   *  accessible name. */
  text: string;
  /** Optional extra classes for the inline wrapper (e.g. spacing). */
  className?: string;
}

export function InfoHint({ text, className }: InfoHintProps) {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [tapped, setTapped] = useState(false);
  const panelId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  // A touch tap fires pointerdown → focus → click. Without this guard the
  // focus would open the tooltip and the tap-toggle click would immediately
  // close it. Recording the pointer press lets us suppress the focus-open for
  // pointer interactions while keeping it for genuine keyboard focus.
  const pointerFocus = useRef(false);

  const open = hovered || focused || tapped;

  const closeAll = () => {
    setHovered(false);
    setFocused(false);
    setTapped(false);
  };

  // Close on a pointer press anywhere outside the component (tap/click-outside).
  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (e: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        closeAll();
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [open]);

  return (
    <span
      ref={rootRef}
      className={`relative inline-flex${className ? ` ${className}` : ''}`}
      // Hover handlers live on the wrapper so moving the pointer from the
      // trigger onto the panel keeps it open (WCAG "hoverable"). Leaving also
      // clears a prior tap so the tooltip can never stay stuck open.
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => {
        setHovered(false);
        setTapped(false);
      }}
    >
      <button
        type="button"
        aria-label={text}
        aria-describedby={open ? panelId : undefined}
        onPointerDown={() => {
          pointerFocus.current = true;
        }}
        onFocus={() => {
          if (!pointerFocus.current) setFocused(true);
        }}
        onBlur={() => {
          setFocused(false);
          setTapped(false);
          pointerFocus.current = false;
        }}
        onClick={() => setTapped((t) => !t)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && open) {
            // Dismiss the tooltip without letting Escape bubble to an
            // enclosing dialog's close handler.
            e.stopPropagation();
            closeAll();
          }
        }}
        className="inline-flex items-center text-bambu-gray/60 hover:text-bambu-gray transition-colors cursor-help rounded focus:outline-none focus-visible:ring-2 focus-visible:ring-bambu-green/50"
      >
        <Info className="w-3.5 h-3.5" aria-hidden="true" />
      </button>
      {open && (
        <span
          id={panelId}
          role="tooltip"
          className="absolute top-full left-0 mt-1 z-10 w-max max-w-xs whitespace-normal rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary px-2 py-1 text-xs font-normal text-white shadow-lg"
        >
          {text}
        </span>
      )}
    </span>
  );
}
