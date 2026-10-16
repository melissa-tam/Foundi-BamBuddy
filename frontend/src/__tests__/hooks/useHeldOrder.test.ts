/**
 * The touch view's order holds still while the screen is in use and re-sorts once it
 * has been idle for the hold window.
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TOUCH_HOLD_MS, useHeldOrder } from '../../hooks/useHeldOrder';

describe('useHeldOrder', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('follows the fresh order while nobody touches the screen', () => {
    const { result, rerender } = renderHook(({ fresh }) => useHeldOrder(fresh), {
      initialProps: { fresh: [1, 2, 3] },
    });
    rerender({ fresh: [3, 1, 2] });
    expect(result.current.order).toEqual([3, 1, 2]);
  });

  it('freezes on a touch, then re-sorts after the idle window', () => {
    const { result, rerender } = renderHook(({ fresh }) => useHeldOrder(fresh), {
      initialProps: { fresh: [1, 2, 3] },
    });
    act(() => result.current.hold());
    rerender({ fresh: [3, 1, 2] });
    expect(result.current.order).toEqual([1, 2, 3]);

    act(() => vi.advanceTimersByTime(TOUCH_HOLD_MS - 1));
    expect(result.current.order).toEqual([1, 2, 3]);

    act(() => vi.advanceTimersByTime(1));
    expect(result.current.order).toEqual([3, 1, 2]);
  });

  it('extends the hold on every touch', () => {
    const { result, rerender } = renderHook(({ fresh }) => useHeldOrder(fresh), {
      initialProps: { fresh: [1, 2] },
    });
    act(() => result.current.hold());
    rerender({ fresh: [2, 1] });
    act(() => vi.advanceTimersByTime(TOUCH_HOLD_MS - 1000));
    act(() => result.current.hold());
    act(() => vi.advanceTimersByTime(TOUCH_HOLD_MS - 1000));
    expect(result.current.order).toEqual([1, 2]);
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current.order).toEqual([2, 1]);
  });
});
