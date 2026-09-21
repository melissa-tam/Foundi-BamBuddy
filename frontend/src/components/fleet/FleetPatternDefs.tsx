/**
 * The Fleet tab's SVG pattern definitions — the third encoding channel.
 *
 * Hue and a luminance ladder carry every class; the pattern is what survives a
 * greyscale print, a deuteranopic reader and a band that has been squeezed to
 * three pixels: **down = 45° hatch, maintenance = dots, no data = sparse
 * hatch**, all at 2 px strokes.
 *
 * Mounted ONCE per page. `<defs>` are document-scoped, so every recharts series
 * on the tab references the same three ids — `fill={patternFill('hatch')}` —
 * and the ids themselves live in `utils/fleetMetrics.ts` beside the palette
 * they belong to, not in this file, so a legend swatch and a chart band cannot
 * end up naming different patterns.
 *
 * HTML surfaces (matrix cells, legend swatches) do NOT use these: they take the
 * CSS equivalents from `FLEET_PATTERN_CSS`, which inherit `currentColor` and so
 * pick up the text colour already proven legible on an opaque heat tile. These
 * defs are drawn on the chart ground instead, where `--text-primary` is the
 * right stroke in both themes.
 *
 * The element is `aria-hidden` and zero-sized: it is a paint server, not
 * content.
 */
import { FLEET_PATTERN_IDS } from '../../utils/fleetMetrics';

/** The pattern stroke. Theme-derived, because the chart ground is too. */
const STROKE = 'var(--text-primary)';

export function FleetPatternDefs() {
  return (
    <svg
      aria-hidden="true"
      focusable="false"
      width={0}
      height={0}
      style={{ position: 'absolute', width: 0, height: 0 }}
    >
      <defs>
        {/* 45° hatch — down. Two diagonals per tile so the repeat is seamless
            across the tile boundary rather than showing a gap at the corner. */}
        <pattern
          id={FLEET_PATTERN_IDS.hatch}
          width={6}
          height={6}
          patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)"
        >
          <line x1={0} y1={0} x2={0} y2={6} stroke={STROKE} strokeWidth={2} />
        </pattern>

        {/* Dots — maintenance. A declared hold reads as deliberate; a dotted
            fill is the one texture that does not look like damage. */}
        <pattern id={FLEET_PATTERN_IDS.dots} width={6} height={6} patternUnits="userSpaceOnUse">
          <circle cx={1.5} cy={1.5} r={1} fill={STROKE} />
        </pattern>

        {/* Sparse hatch — no data. Same stroke, triple the gap: visibly the
            same family as `down` but visibly thinner, so a reader never takes
            an unobserved stretch for an outage. */}
        <pattern
          id={FLEET_PATTERN_IDS.sparse}
          width={12}
          height={12}
          patternUnits="userSpaceOnUse"
          patternTransform="rotate(45)"
        >
          <line x1={0} y1={0} x2={0} y2={12} stroke={STROKE} strokeWidth={2} />
        </pattern>
      </defs>
    </svg>
  );
}

export default FleetPatternDefs;
