/**
 * Keep a list's order still while a person is using it: `hold()` (called on every
 * touch, click or key on the surface) freezes the order on screen, and the list
 * re-sorts to `fresh` once `holdMs` passes with no further `hold()`. A printer added
 * while held is appended; one removed drops out (`utils/touchOrder.holdOrder`).
 */
import { useEffect, useRef, useState } from 'react';
import { holdOrder } from '../utils/touchOrder';

/** Idle time after the last touch before the touch view re-sorts. */
export const TOUCH_HOLD_MS = 30_000;

export interface HeldOrder {
  order: number[];
  /** The screen is being used: keep the current order until it has been idle. */
  hold: () => void;
}

export function useHeldOrder(fresh: number[], holdMs: number = TOUCH_HOLD_MS): HeldOrder {
  const [held, setHeld] = useState<number[] | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const order = held === null ? fresh : holdOrder(held, fresh);

  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

  const hold = () => {
    setHeld(order);
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      timer.current = null;
      setHeld(null);
    }, holdMs);
  };

  return { order, hold };
}
