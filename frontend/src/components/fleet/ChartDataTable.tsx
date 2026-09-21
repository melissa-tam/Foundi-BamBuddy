/**
 * The data behind a chart, as a real table — ONE component for the whole Fleet
 * grid.
 *
 * Every widget's "Show data" button swaps its chart for this, fed by the SAME
 * rows the chart draws. That is the point of it being one component and one
 * feed: a table built from a second derivation is a second answer, and the
 * reader who opened it did so precisely because they did not trust the picture.
 *
 * It is also the chart's accessible alternative. A recharts plot is an
 * `role="img"` with a name; the numbers behind it live here, in a `<table>`
 * with a `<caption>`, `<th scope>` headers carrying their units, and
 * `tabular-nums` so a column of figures lines up. The scroll container is
 * focusable and named, because a region that scrolls has to be reachable
 * without a pointer (WCAG 2.1.1).
 *
 * **A partly observed bucket is marked, not hidden.** The marker is the same
 * sparse hatch the charts paint, drawn from `FLEET_PATTERN_CSS` so the table
 * and the plot cannot drift into two different textures, and it carries a real
 * accessible name rather than being colour and texture alone.
 */
import { useTranslation } from 'react-i18next';
import { InfoHint } from '../ui/InfoHint';
import {
  FLEET_ABSENCE_COLOR,
  FLEET_ABSENCE_TEXT,
  FLEET_PATTERN_CSS,
  SECONDARY_TEXT_CLASS,
} from '../../utils/fleetMetrics';

/** One column: its heading, its unit, and how it reads a value out of a row. */
export interface ChartDataColumn<Values> {
  /** React key and test handle. Never rendered. */
  key: string;
  header: string;
  /** Shown after the heading, e.g. `h` or `printers`. Omit where the heading says it. */
  unit?: string;
  format: (values: Values) => string;
}

/** One row: its own heading, the values the columns read, and its honesty flag. */
export interface ChartDataRow<Values> {
  key: string;
  header: string;
  /** The state recorder covered less of this bucket than elapsed. */
  partial?: boolean;
  values: Values;
}

export interface ChartDataTableProps<Values> {
  /** What this table is. Rendered as the `<caption>`. */
  caption: string;
  /** The first column's heading — what the row headers are. */
  rowHeader: string;
  columns: readonly ChartDataColumn<Values>[];
  rows: readonly ChartDataRow<Values>[];
  /** The window's own figures, rendered as a footer row. */
  totals?: ChartDataRow<Values>;
}

/**
 * The partly-observed marker: the sparse hatch, named.
 *
 * `color` is set to the swatch's paired text colour because `FLEET_PATTERN_CSS`
 * strokes with `currentColor` — the pattern then inherits a stroke already
 * proven legible on that exact tile.
 */
function PartialMarker({ label }: { label: string }) {
  return (
    <span
      role="img"
      aria-label={label}
      className="ml-1.5 inline-block h-3 w-3 shrink-0 rounded-sm align-middle"
      style={{
        backgroundColor: FLEET_ABSENCE_COLOR.unobserved,
        color: FLEET_ABSENCE_TEXT.unobserved,
        ...FLEET_PATTERN_CSS.sparse,
      }}
    />
  );
}

export function ChartDataTable<Values>({
  caption,
  rowHeader,
  columns,
  rows,
  totals,
}: ChartDataTableProps<Values>) {
  const { t } = useTranslation();
  const partialLabel = t('fleetMetrics.class.unobserved');
  const anyPartial = rows.some((row) => row.partial === true);

  return (
    <div
      className="w-full overflow-x-auto"
      // A horizontally scrolling region needs to be operable from the keyboard.
      tabIndex={0}
      role="group"
      aria-label={caption}
    >
      <table className="w-full min-w-max border-collapse text-xs tabular-nums">
        <caption className={`mb-2 text-left text-xs ${SECONDARY_TEXT_CLASS}`}>
          <span className="inline-flex items-center gap-1">
            {caption}
            {anyPartial && <InfoHint text={t('fleetMetrics.hints.unobserved')} />}
          </span>
        </caption>
        <thead>
          <tr className="border-b border-bambu-dark-tertiary">
            <th scope="col" className={`py-1.5 pr-3 text-left font-medium ${SECONDARY_TEXT_CLASS}`}>
              {rowHeader}
            </th>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`py-1.5 pl-3 text-right font-medium ${SECONDARY_TEXT_CLASS}`}
              >
                {column.unit === undefined ? column.header : `${column.header} (${column.unit})`}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} className="border-b border-bambu-dark-tertiary/50">
              <th
                scope="row"
                className="whitespace-nowrap py-1.5 pr-3 text-left font-normal text-white"
              >
                {row.header}
                {row.partial === true && <PartialMarker label={partialLabel} />}
              </th>
              {columns.map((column) => (
                <td key={column.key} className="py-1.5 pl-3 text-right text-white">
                  {column.format(row.values)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {totals && (
          <tfoot>
            <tr className="border-t border-bambu-dark-tertiary">
              <th
                scope="row"
                className="whitespace-nowrap py-1.5 pr-3 text-left font-medium text-white"
              >
                {totals.header}
              </th>
              {columns.map((column) => (
                <td key={column.key} className="py-1.5 pl-3 text-right font-medium text-white">
                  {column.format(totals.values)}
                </td>
              ))}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}

export default ChartDataTable;
