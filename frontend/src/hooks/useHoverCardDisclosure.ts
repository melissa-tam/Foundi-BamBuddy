import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from 'react';
import { FOCUSABLE_SELECTOR } from './useFocusTrap';

/** Open delay (ms) — swallows a pointer flying across a row of slots. */
const OPEN_DELAY_MS = 80;
/** Close delay (ms) — lets the pointer cross the gap from trigger to card. */
const CLOSE_DELAY_MS = 100;

/** Props spread onto the always-mounted trigger element. */
export interface HoverCardTriggerProps {
  tabIndex: number;
  role: 'button';
  'aria-haspopup': 'dialog';
  'aria-expanded': boolean;
  'aria-controls': string;
  'aria-label': string;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onFocus: () => void;
  onBlur: (event: ReactFocusEvent<HTMLElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
}

/** Props spread onto the portaled card element (the popover root). */
export interface HoverCardCardProps {
  id: string;
  role: 'dialog';
  'aria-label': string;
  tabIndex: -1;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onBlur: (event: ReactFocusEvent<HTMLElement>) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void;
}

export interface HoverCardDisclosure {
  isVisible: boolean;
  open: () => void;
  close: () => void;
  triggerProps: HoverCardTriggerProps;
  cardProps: HoverCardCardProps;
  cardId: string;
}

export interface HoverCardDisclosureOptions {
  /** The always-mounted element the card is anchored to and named after. */
  triggerRef: RefObject<HTMLElement | null>;
  /** The portaled popover root. Portaled is fine — containment is DOM-based. */
  cardRef: RefObject<HTMLElement | null>;
  /**
   * Accessible name for BOTH the trigger and the card. Required: every action a
   * slot has lives inside the popover, so an unnamed trigger is an unreachable
   * action list.
   */
  label: string;
  /** Trigger is inert: no hover open, no tab stop, no keyboard open. */
  disabled?: boolean;
}

/**
 * Disclosure state + wiring for a NON-MODAL hover card whose only actions live
 * inside the popover (the AMS slot cards).
 *
 * Pointer behaviour is the historical one (80 ms open / 100 ms close, trigger
 * and card both keep it open). What it adds is the keyboard path the cards
 * never had: the trigger is a real tab stop that opens on focus WITHOUT moving
 * focus (an offer, not an interruption), Enter/Space opens AND moves focus to
 * the first control, focus leaving trigger+card closes, and Escape closes from
 * anywhere while open (WCAG 2.2 1.4.13 — dismissible, hover-opened included).
 *
 * Deliberately NOT a focus trap (`useFocusTrap`): a non-modal popover must not
 * hold the Tab cycle. Tabbing past the last control closes the card and
 * continues at the trigger's own successor in the document tab order — which
 * has to be computed, because the card is portaled to the end of `body` and
 * native sequential navigation would otherwise walk out of the page.
 *
 * Escape is scoped: a `ui/Modal` opened FROM the card owns the key while focus
 * is inside it, so the popover never swallows the modal's own dismissal.
 */
export function useHoverCardDisclosure({
  triggerRef,
  cardRef,
  label,
  disabled = false,
}: HoverCardDisclosureOptions): HoverCardDisclosure {
  const [isVisible, setIsVisible] = useState(false);
  const cardId = useId();
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Set by a keyboard open so the card, once mounted, takes focus. A pointer or
  // focus open never sets it.
  const focusOnOpenRef = useRef(false);
  // Guards the ONE focus move that must not re-open the card: returning focus
  // to the trigger as part of dismissing it (Escape, Shift+Tab back out).
  // Without it the trigger's own focus-open handler reopens what just closed.
  const skipFocusOpenRef = useRef(false);

  const clearTimer = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  const open = useCallback(() => {
    clearTimer();
    setIsVisible(true);
  }, [clearTimer]);

  const close = useCallback(() => {
    clearTimer();
    focusOnOpenRef.current = false;
    setIsVisible(false);
  }, [clearTimer]);

  useEffect(
    () => () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    },
    [],
  );

  /** Is the keyboard currently INSIDE the disclosure (trigger or card)? */
  const containsFocus = useCallback(() => {
    const active = document.activeElement;
    if (!active) return false;
    return !!triggerRef.current?.contains(active) || !!cardRef.current?.contains(active);
  }, [cardRef, triggerRef]);

  const focusFirstControl = useCallback((): boolean => {
    const card = cardRef.current;
    if (!card) return false;
    const first = card.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    (first ?? card).focus();
    return true;
  }, [cardRef]);

  /**
   * Focus the trigger's successor in the document tab order, skipping the card
   * itself. The card lives in a portal at the end of `body`, so "the next slot"
   * is only reachable by computing it.
   */
  const focusAfterTrigger = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    const card = cardRef.current;
    const tabbables = Array.from(
      document.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    ).filter((el) => !card?.contains(el));
    const index = tabbables.indexOf(trigger);
    const next = index >= 0 ? tabbables[index + 1] : undefined;
    (next ?? trigger).focus();
  }, [cardRef, triggerRef]);

  /** Focus the trigger as part of a dismissal — the focus must not re-open it. */
  const dismissToTrigger = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return;
    skipFocusOpenRef.current = true;
    // focus() dispatches synchronously, so the guard is consumed before this
    // returns; clearing it here also covers "already focused" (no event).
    trigger.focus();
    skipFocusOpenRef.current = false;
  }, [triggerRef]);

  const handleMouseEnter = useCallback(() => {
    if (disabled) return;
    clearTimer();
    timeoutRef.current = setTimeout(() => setIsVisible(true), OPEN_DELAY_MS);
  }, [clearTimer, disabled]);

  const handleMouseLeave = useCallback(() => {
    clearTimer();
    timeoutRef.current = setTimeout(() => {
      // The pointer leaving closes it, exactly as it always has — a card that
      // outlived the pointer because a click had left focus on one of its
      // buttons would never close again. What is new is that the keyboard is
      // not dropped on the floor with it: a focused control about to be
      // unmounted hands focus back to the slot instead of to `body`.
      const restore = containsFocus();
      close();
      if (restore) dismissToTrigger();
    }, CLOSE_DELAY_MS);
  }, [clearTimer, close, containsFocus, dismissToTrigger]);

  const handleTriggerFocus = useCallback(() => {
    if (disabled || skipFocusOpenRef.current) return;
    open();
  }, [disabled, open]);

  const handleTriggerKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (disabled) return;
      // The card is portaled to `body` but rendered from this element's JSX, so
      // React bubbles the card's OWN key events to here. Enter on a button
      // inside the card must not be swallowed by the trigger's preventDefault.
      if (event.target !== event.currentTarget) return;
      if (event.key !== 'Enter' && event.key !== ' ' && event.key !== 'Spacebar') return;
      // Space would scroll the page; Enter would submit an enclosing form.
      event.preventDefault();
      if (isVisible && focusFirstControl()) return;
      focusOnOpenRef.current = true;
      open();
    },
    [disabled, focusFirstControl, isVisible, open],
  );

  /** Close when focus leaves BOTH the trigger and the card. */
  const handleBlur = useCallback(
    (event: ReactFocusEvent<HTMLElement>) => {
      const next = event.relatedTarget as Node | null;
      if (next && (triggerRef.current?.contains(next) || cardRef.current?.contains(next))) return;
      close();
    },
    [cardRef, close, triggerRef],
  );

  const handleCardKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (event.key !== 'Tab') return;
      const card = cardRef.current;
      if (!card) return;
      const focusables = Array.from(card.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      const active = document.activeElement;
      if (event.shiftKey) {
        if (focusables.length === 0 || active === focusables[0] || active === card) {
          event.preventDefault();
          close();
          dismissToTrigger();
        }
        return;
      }
      if (focusables.length === 0 || active === focusables[focusables.length - 1]) {
        event.preventDefault();
        close();
        focusAfterTrigger();
      }
    },
    [cardRef, close, dismissToTrigger, focusAfterTrigger],
  );

  // Consume a keyboard open: the card is mounted now, so hand it the focus.
  useEffect(() => {
    if (!isVisible || !focusOnOpenRef.current) return;
    focusOnOpenRef.current = false;
    focusFirstControl();
  }, [focusFirstControl, isVisible]);

  // Escape closes from anywhere while the card is open (1.4.13 dismissible),
  // including a card opened by hover alone.
  useEffect(() => {
    if (!isVisible) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      const active = document.activeElement;
      // A modal opened from inside the card owns its own Escape.
      if (active instanceof Element && active.closest('[role="dialog"][aria-modal="true"]')) return;
      // Return focus to the trigger only when the keyboard is here (or nowhere)
      // — never steal it from whatever the operator is actually working in.
      const restore = !active || active === document.body || containsFocus();
      close();
      if (restore) dismissToTrigger();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [close, containsFocus, dismissToTrigger, isVisible]);

  return {
    isVisible,
    open,
    close,
    cardId,
    triggerProps: {
      tabIndex: disabled ? -1 : 0,
      role: 'button',
      'aria-haspopup': 'dialog',
      'aria-expanded': isVisible,
      'aria-controls': cardId,
      'aria-label': label,
      onMouseEnter: handleMouseEnter,
      onMouseLeave: handleMouseLeave,
      onFocus: handleTriggerFocus,
      onBlur: handleBlur,
      onKeyDown: handleTriggerKeyDown,
    },
    cardProps: {
      id: cardId,
      role: 'dialog',
      'aria-label': label,
      tabIndex: -1,
      onMouseEnter: handleMouseEnter,
      onMouseLeave: handleMouseLeave,
      onBlur: handleBlur,
      onKeyDown: handleCardKeyDown,
    },
  };
}
