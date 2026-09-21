/**
 * Sparkline — the trend column of the fleet summary card.
 *
 * Inline `<svg><polyline>`, 72 × 20, and deliberately nothing else: no chart
 * library, no axes, no tooltip, no animation. It is read as a SHAPE beside the
 * figure it belongs to, and a reader who wants the numbers opens the matrix.
 *
 * Two rules the shape has to keep honest:
 *
 * - **A null point BREAKS the line.** A bucket the recorder did not cover has
 *   no figure, and drawing straight through it would invent one — the same lie
 *   the matrix refuses by hatching a partly observed cell instead of showing a
 *   zero. Consecutive non-null runs are drawn as separate polylines, so the gap
 *   is visible as a gap.
 * - **A lone point is a dot.** A one-point polyline renders nothing at all in
 *   SVG, so a window with exactly one observed bucket would silently show an
 *   empty cell.
 *
 * Colour comes from `currentColor`: the cell that hosts it sets the text
 * colour, so the line can never be an accent (which in this app is
 * user-selectable and therefore cannot encode data).
 */
import type { CSSProperties } from 'react';

export interface SparklineProps {
  /** One value per bucket, in order. `null` = a bucket with no figure. */
  points: readonly (number | null)[];
  /**
   * Accessible name — REQUIRED. A `role="img"` with no name is announced as
   * nothing at all, which is worse than the decorative image it replaced.
   */
  ariaLabel: string;
  width?: number;
  height?: number;
  className?: string;
  style?: CSSProperties;
}

const DEFAULT_WIDTH = 72;
const DEFAULT_HEIGHT = 20;
/** Room for the stroke's own width at the extremes, so nothing is clipped. */
const PADDING = 2;
const STROKE_WIDTH = 1.5;
const DOT_RADIUS = 1.5;

/** One run of consecutive non-null points, with the index each came from. */
interface Segment {
  from: number;
  points: { index: number; value: number }[];
}

function toSegments(points: readonly (number | null)[]): Segment[] {
  const segments: Segment[] = [];
  let run: { index: number; value: number }[] = [];
  points.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) {
      if (run.length > 0) segments.push({ from: run[0].index, points: run });
      run = [];
      return;
    }
    run.push({ index, value });
  });
  if (run.length > 0) segments.push({ from: run[0].index, points: run });
  return segments;
}

export function Sparkline({
  points,
  ariaLabel,
  width = DEFAULT_WIDTH,
  height = DEFAULT_HEIGHT,
  className,
  style,
}: SparklineProps) {
  const segments = toSegments(points);
  const values = segments.flatMap((segment) => segment.points.map((point) => point.value));
  // Nothing was ever observed in this window: the cell draws no line — a flat
  // one would read as "steady at zero" — but it still OCCUPIES its 72 × 20 box.
  // Returning null instead collapsed the row, so the whole summary card grew
  // when history arrived and shoved the page down; and an empty graphic has
  // nothing to announce, so it carries no `role="img"` and no name.
  if (values.length === 0) {
    return (
      <svg
        aria-hidden="true"
        width={width}
        height={height}
        className={className}
        style={style}
        focusable="false"
      />
    );
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const lastIndex = points.length - 1;
  const innerWidth = width - PADDING * 2;
  const innerHeight = height - PADDING * 2;

  const x = (index: number): number =>
    lastIndex <= 0 ? width / 2 : PADDING + (index / lastIndex) * innerWidth;
  // A flat series sits on the mid-line: with no span there is no "high" to
  // put it at, and pinning it to the top or bottom would imply one.
  const y = (value: number): number =>
    span === 0 ? PADDING + innerHeight / 2 : PADDING + (1 - (value - min) / span) * innerHeight;

  const round = (value: number): string => value.toFixed(2);

  return (
    <svg
      role="img"
      aria-label={ariaLabel}
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={className}
      style={style}
      focusable="false"
    >
      {segments.map((segment) =>
        segment.points.length === 1 ? (
          <circle
            key={segment.from}
            cx={round(x(segment.points[0].index))}
            cy={round(y(segment.points[0].value))}
            r={DOT_RADIUS}
            fill="currentColor"
          />
        ) : (
          <polyline
            key={segment.from}
            points={segment.points
              .map((point) => `${round(x(point.index))},${round(y(point.value))}`)
              .join(' ')}
            fill="none"
            stroke="currentColor"
            strokeWidth={STROKE_WIDTH}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ),
      )}
    </svg>
  );
}
