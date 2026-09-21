/**
 * Marks a chart paints on TOP of its series — the shapes that are neither the
 * data nor the chrome around it.
 *
 * Its own module rather than a corner of `ChartFrame` because these are
 * factories that RETURN a renderer, not components: a file that exports both
 * loses fast refresh (`react-refresh/only-export-components`), and the fix is a
 * boundary rather than a suppression.
 */

import type { ReactElement } from 'react';

/** Radius of an isolated point, in px: visible, without reading as a data band. */
const ISOLATED_DOT_RADIUS = 2.5;

/** As much of a recharts dot payload as this module needs. */
interface DotPayload {
  bucketStart?: unknown;
}

interface DotRenderProps {
  payload?: unknown;
  cx?: number;
  cy?: number;
}

/**
 * A `dot` prop that marks only the points a LINE cannot reach.
 *
 * `isolatedPointKeys` (in `utils/fleetMetrics`) decides which buckets those
 * are; this turns that verdict into a mark. Without it a series with one
 * non-null bucket draws a zero-length path — nothing at all on screen — while
 * the legend beside it promises a line. That is the ordinary state of every
 * observed-only series during an instance's first days, so the legend was
 * lying on the most common window there is.
 *
 * Keyed on the row recharts hands back, never on its `index`: recharts filters
 * the null points out before rendering marks, so its index counts only the
 * survivors and a row-indexed lookup silently answers about the wrong bucket.
 * Nothing but the browser catches that — jsdom lays out no chart at all.
 *
 * A run of two or more points keeps a plain line and no dots: these are a
 * fallback for "this point has no neighbour", never decoration.
 */
export function isolatedDot(
  isolatedBuckets: ReadonlySet<string>,
  color: string,
): (props: DotRenderProps) => ReactElement {
  return ({ payload, cx, cy }) => {
    const bucketStart = (payload as DotPayload | undefined)?.bucketStart;
    const isolated = typeof bucketStart === 'string' && isolatedBuckets.has(bucketStart);
    // recharts wants an element back from a `dot` renderer, so a skipped point
    // returns an empty one rather than turning the series' dots back on.
    if (!isolated || cx === undefined || cy === undefined) return <g />;
    return <circle cx={cx} cy={cy} r={ISOLATED_DOT_RADIUS} fill={color} stroke="none" />;
  };
}
