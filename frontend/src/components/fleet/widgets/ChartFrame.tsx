/**
 * The furniture every Fleet grid widget shares: the chart/table switch, the
 * legend, and the bar shape that carries the partly-observed hatch.
 *
 * Why a frame rather than six copies: the "Show data" toggle is an honesty
 * mechanism, not a convenience. It has to behave identically on every widget —
 * same control, same accessible name, same state semantics — or a reader learns
 * it on one chart and is surprised by the next. The same argument makes the
 * legend one component: the swatch is where the THIRD encoding channel (the
 * pattern) is shown, and a legend that drew a plain colour chip would quietly
 * drop it.
 */
import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Rectangle, type BarShapeProps } from 'recharts';
import { Button } from '../../Button';
import { InfoHint } from '../../ui/InfoHint';
import {
  FLEET_PATTERN_CSS,
  SECONDARY_TEXT_CLASS,
  patternFill,
  type FleetPattern,
} from '../../../utils/fleetMetrics';

export interface ChartFrameProps {
  /** The chart's accessible name. Names the `role="img"` the plot lives in. */
  title: string;
  /** The plot. Rendered inside the named figure. */
  chart: ReactNode;
  /** The same numbers as a table. Rendered in place of the plot when toggled. */
  table: ReactNode;
  /** The band identities. Omitted on a single-series chart, which names itself. */
  legend?: ReactNode;
  /** Figures that lead the widget — shown whether the chart or the table is up. */
  headline?: ReactNode;
  /** The widget's own controls, e.g. a per-printer toggle. */
  controls?: ReactNode;
  /** Honest facts that are not in the plot, e.g. episodes that never finished. */
  footer?: ReactNode;
  /** The series has nothing in this window. */
  isEmpty: boolean;
  /**
   * The chart is an opaque graphic (an SVG plot), so it is named as one image
   * and the data table is its accessible alternative. Pass `false` for a chart
   * drawn in HTML, whose own text must stay readable — it is then a named
   * `<figure>` instead, which carries a name WITHOUT hiding its contents.
   */
  graphic?: boolean;
}

export function ChartFrame({
  title,
  chart,
  table,
  legend,
  headline,
  controls,
  footer,
  isEmpty,
  graphic = true,
}: ChartFrameProps) {
  const { t } = useTranslation();
  const [showData, setShowData] = useState(false);

  return (
    <div className="space-y-3">
      {headline}
      {!isEmpty && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">{controls}</div>
          <Button
            variant="secondary"
            size="sm"
            aria-pressed={showData}
            onClick={() => setShowData((shown) => !shown)}
          >
            {showData ? t('fleetMetrics.widgets.showChart') : t('fleetMetrics.widgets.showData')}
          </Button>
        </div>
      )}

      {isEmpty ? (
        <p role="status" className={`py-6 text-center text-sm ${SECONDARY_TEXT_CLASS}`}>
          {t('fleetMetrics.states.empty')}
        </p>
      ) : showData ? (
        table
      ) : (
        <>
          {graphic ? (
            <div role="img" aria-label={title}>
              {chart}
            </div>
          ) : (
            <figure className="m-0" aria-label={title}>
              {chart}
            </figure>
          )}
          {legend}
        </>
      )}

      {footer}
    </div>
  );
}

/** One legend entry: what the band is, and every channel that says so. */
export interface ChartLegendEntry {
  key: string;
  label: string;
  /** The opaque band colour. Omitted on a band that is pattern only. */
  color?: string;
  /** The text colour proven legible on `color`; the pattern strokes with it. */
  textColor?: string;
  pattern?: FleetPattern;
  /** A line series rather than a band — the swatch is drawn as its own stroke. */
  line?: 'solid' | 'dashed';
}

/**
 * The legend. Always present once a chart draws two or more series, because
 * identity is then never colour alone — each swatch carries the band's colour
 * AND its pattern, and the name beside it carries neither.
 *
 * A line series gets a line swatch, not a filled chip: a reader looking for the
 * dashed roster ceiling should be able to find the dash in the legend.
 */
export function ChartLegend({ entries }: { entries: readonly ChartLegendEntry[] }) {
  return (
    <ul className={`flex flex-wrap gap-x-4 gap-y-1 text-xs ${SECONDARY_TEXT_CLASS}`}>
      {entries.map((entry) => (
        <li key={entry.key} className="flex items-center gap-1.5">
          {entry.line === undefined ? (
            <span
              aria-hidden="true"
              className="inline-block h-3 w-3 shrink-0 rounded-sm border border-bambu-dark-tertiary"
              style={{
                backgroundColor: entry.color,
                color: entry.textColor,
                ...(entry.pattern ? FLEET_PATTERN_CSS[entry.pattern] : {}),
              }}
            />
          ) : (
            <span
              aria-hidden="true"
              className="inline-block h-0 w-3 shrink-0"
              style={{ borderTop: `2px ${entry.line} ${entry.color ?? 'currentColor'}` }}
            />
          )}
          {entry.label}
        </li>
      ))}
    </ul>
  );
}

/** One figure a widget leads with: what it is, what it reads, and its footnote. */
export interface HeadlineFigure {
  key: string;
  label: string;
  value: string;
  /** A qualifier the figure is not honest without, e.g. the kind a median is of. */
  detail?: string;
  /** Supplementary detail. Rides a tooltip, never the primary surface. */
  hint?: string;
}

/**
 * The figures a widget leads with, as a description list — a label/value pair
 * is exactly what `<dt>`/`<dd>` are, and the pairing survives a screen reader.
 */
export function HeadlineFigures({ figures }: { figures: readonly HeadlineFigure[] }) {
  return (
    <dl className="flex flex-wrap gap-x-6 gap-y-2">
      {figures.map((figure) => (
        <div key={figure.key} className="min-w-0">
          <dt className={`flex items-center gap-1 text-xs ${SECONDARY_TEXT_CLASS}`}>
            {figure.label}
            {figure.hint !== undefined && <InfoHint text={figure.hint} />}
          </dt>
          <dd className="text-lg font-semibold tabular-nums text-white">
            {figure.value}
            {figure.detail !== undefined && (
              <span className={`ml-2 text-xs font-normal ${SECONDARY_TEXT_CLASS}`}>
                {figure.detail}
              </span>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Does this row say the recorder fell short of the bucket's elapsed time? */
function isPartialRow(payload: unknown): boolean {
  return (
    typeof payload === 'object' &&
    payload !== null &&
    'bucketPartial' in payload &&
    (payload as { bucketPartial?: unknown }).bucketPartial === true
  );
}

export interface PartialAwareBarProps extends BarShapeProps {
  /** The band's own colour. Passed explicitly: the computed props do not type it. */
  fill: string;
}

/**
 * A stacked-bar segment that hatches itself when its bucket is partly observed.
 *
 * One rectangle for the colour and, over it, the same rectangle filled with the
 * shared sparse `<pattern>` — so the texture is composed at PAINT time rather
 * than by defining a second, colour-bearing pattern that would have to repeat
 * the geometry `FleetPatternDefs` already owns and would then drift from it.
 */
export function PartialAwareBar({ payload, x, y, width, height, radius, fill }: PartialAwareBarProps) {
  const rect = { x, y, width, height, radius };
  return (
    <>
      <Rectangle {...rect} fill={fill} />
      {isPartialRow(payload) && <Rectangle {...rect} fill={patternFill('sparse')} />}
    </>
  );
}
