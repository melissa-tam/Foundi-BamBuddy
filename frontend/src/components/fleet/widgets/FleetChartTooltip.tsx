/**
 * THE tooltip for every Fleet chart.
 *
 * ## Why it is not recharts' own
 *
 * Recharts paints each tooltip row in its series' raw `fill`, and a fill chosen
 * to be legible AS A BAND is not legible AS TEXT — they are different jobs with
 * different thresholds. Measured on the dark neutral ground, idle `#737373`
 * came out at 2.97:1 and down `#ef4444` at 3.75:1; on a light ground printing
 * `#4ade80` was 1.74:1. WCAG asks 4.5:1 for body text, so every one of those
 * rows failed, and the brightest series were the worst.
 *
 * The fix is to stop using colour as the text and start using it as a SWATCH:
 * the band's identity moves into a 10 px chip beside the row — where a colour
 * only has to be distinguishable, not readable — and the words go in
 * `--text-primary`, which is the one colour the theme guarantees against its
 * own ground. The swatch carries the series' PATTERN too, so the second
 * encoding channel survives into the tooltip instead of stopping at the plot.
 *
 * ## The header
 *
 * The bucket's FULL site-local label ("Jun 22, 2026", "Week of …", the hour),
 * not the axis tick. The axis is allowed to say "5" because it has twenty-nine
 * neighbours giving it context; a tooltip has none, and "5" on its own names
 * nothing. It comes off the row the chart was drawn from, so it is the same
 * string the data table prints for that bucket.
 */
import { CHART_TOOLTIP_CONTENT_STYLE } from '../../../utils/chartChrome';
import {
  FLEET_PATTERN_CSS,
  HEAT_TEXT_DARK,
  type FleetPattern,
} from '../../../utils/fleetMetrics';

/** What the tooltip needs off the row a chart was built from. */
interface BucketRowShape {
  fullLabel?: unknown;
  axisLabel?: unknown;
}

/**
 * One series' entry in a tooltip payload.
 *
 * Declared here rather than imported as recharts' own `TooltipContentProps`:
 * the `content` render prop is typed against recharts' DEFAULT generics, so a
 * component whose props pin `<number, string>` cannot receive them. Naming the
 * four fields this tooltip actually reads keeps the seam narrow and the call
 * sites free of generic gymnastics.
 */
interface TooltipSeriesEntry {
  dataKey?: unknown;
  name?: unknown;
  value?: unknown;
  color?: string;
  /** Recharts mirrors the series' `tooltipType` here. `'none'` means "not mine". */
  type?: unknown;
  payload?: unknown;
}

export interface FleetChartTooltipProps {
  /** Recharts is hovering a category. False or absent renders nothing. */
  active?: boolean;
  payload?: readonly TooltipSeriesEntry[];
  /** The series' own unit. Hours, counts and average printers all read differently. */
  formatValue: (value: number) => string;
  /**
   * The pattern a series carries, by `dataKey`. Omitted on a chart whose bands
   * are hue-only; supplied wherever the plot itself draws a texture, so the
   * swatch and the band cannot disagree.
   */
  patternFor?: (dataKey: string) => FleetPattern | undefined;
}

/** The bucket's unambiguous name, or the axis tick if a row somehow lacks one. */
function headerOf(payload: readonly TooltipSeriesEntry[] | undefined): string {
  const row = payload?.[0]?.payload as BucketRowShape | undefined;
  if (typeof row?.fullLabel === 'string') return row.fullLabel;
  if (typeof row?.axisLabel === 'string') return row.axisLabel;
  return '';
}

export function FleetChartTooltip({
  active,
  payload,
  formatValue,
  patternFor,
}: FleetChartTooltipProps) {
  if (active !== true || !payload || payload.length === 0) return null;

  const rows = payload.filter(
    (entry) =>
      // A series with no value in this bucket is a GAP, not a zero — recharts
      // still hands it over, and printing it would invent data the chart
      // deliberately refused to draw.
      typeof entry.value === 'number' &&
      // …and a PAINT-ONLY series is not a reading at all. The state chart draws
      // its stack twice, once for colour and once for the pattern overlay, and
      // both carry the same dataKey and the same value; recharts' own tooltip
      // honours the `tooltipType="none"` those overlays declare, but a custom
      // `content` renderer is handed the unfiltered payload and has to honour
      // it itself. Without this every band appeared twice — once named, once
      // as its raw key.
      entry.type !== 'none',
  );
  if (rows.length === 0) return null;

  const header = headerOf(payload);

  return (
    <div style={CHART_TOOLTIP_CONTENT_STYLE} className="px-2.5 py-2 text-xs">
      {header !== '' && <p className="mb-1 font-medium text-[var(--text-primary)]">{header}</p>}
      <ul className="space-y-0.5">
        {rows.map((entry, index) => {
          const key = String(entry.dataKey ?? entry.name ?? '');
          const label = entry.name === undefined ? key : String(entry.name);
          const pattern = patternFor?.(key);
          return (
            // Keyed by POSITION as well as name: a chart may legitimately
            // draw two series off one dataKey, and a duplicate React key
            // silently drops one of them.
            <li key={`${key}:${index}`} className="flex items-center gap-1.5 whitespace-nowrap">
              <span
                aria-hidden="true"
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-[2px]"
                style={{
                  backgroundColor: entry.color,
                  // The pattern strokes with `currentColor`; these bands were
                  // all contrast-checked against dark text, which is the same
                  // choice `FleetSplitBar` makes for the same swatches.
                  color: HEAT_TEXT_DARK,
                  ...(pattern ? FLEET_PATTERN_CSS[pattern] : {}),
                }}
              />
              <span className="text-[var(--text-primary)]">{label}</span>
              <span className="ml-auto pl-2 tabular-nums text-[var(--text-primary)]">
                {formatValue(entry.value as number)}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default FleetChartTooltip;
