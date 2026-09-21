/**
 * One cell of the fleet matrix — the `<td>` itself, its absence treatment and
 * its lens encoding.
 *
 * A separate component from `FleetMatrix` because the grid is an orchestrator
 * (selection, sorting, roving focus, the detail modal) and this is a pure
 * presenter: everything it needs arrives as props, and nothing it renders
 * depends on where in the grid it sits. The `<td>` lives HERE rather than in
 * the grid, so the four absence treatments and the heat tile cannot be applied
 * to one element while `tabIndex` and the width are applied to another.
 *
 * ## The four absences, never conflated
 *
 * `utils/fleetMetrics.cellAbsence` is the single verdict — this file never
 * re-derives one. The treatments:
 *
 *   - `zero` — a dim `0`. The farm earned it.
 *   - `before_recording` — an em dash, with the reason as sr-only text. There
 *     was no recorder yet, so there is no number to be zero.
 *   - `partial` — the sparse hatch over whatever IS known, with the reason as
 *     sr-only text (`No data`, or `Faults and holds only` when the fault
 *     ledger is the bucket's only evidence). A hatched cell may still carry a
 *     real figure: the incident ledger proves fault hours the recorder missed.
 *   - `out_of_fleet` — flat ground, no number. The printer was deactivated.
 *
 * On a lights-out farm the difference between "zero prints because nothing was
 * queued" and "zero prints because nobody was recording" is the whole point,
 * which is why none of these collapses into a blank. The Prints lens is never
 * hatched and never dashed — the print log is complete for its own history —
 * and `cellAbsence` enforces that from the lens argument, so this file simply
 * renders whatever verdict comes back.
 *
 * ## No per-cell `aria-label`
 *
 * An `aria-label` on a `<td>` REPLACES its content for assistive tech and
 * suppresses the row/column header association a real `<table>` gives for
 * free. The cell's own text IS its value; where the column is too narrow to
 * show one (the 14 px hour lens) the value rides sr-only text instead, so the
 * accessible reading is "009-H2C, Sep 3, 4.0" either way.
 */
import type { CSSProperties, KeyboardEvent, Ref } from 'react';
import { useTranslation } from 'react-i18next';
import type { FleetBucket, MatrixCell } from '../../types/fleetMetrics';
import {
  BASIS_LABEL_KEY,
  FLEET_ABSENCE_COLOR,
  FLEET_PATTERN_CSS,
  SECONDARY_TEXT_CLASS,
  TIME_SPLIT_BAND_COLOR,
  cellAbsence,
  foldTimeSplit,
  formatCount,
  formatHours,
  formatPercent,
  hoursDownBand,
  lensRamp,
  printsBand,
  sumMap,
  type AbsenceHeader,
  type FleetLens,
  type ResolvedThemeMode,
  type TimeSplit,
} from '../../utils/fleetMetrics';

const SECONDS_PER_HOUR = 3600;

/**
 * The house focus ring is `ring-bambu-green`, which vanishes against a green
 * Prints tile — the one place in this app where the accent colour and the data
 * colour collide. Matrix cells therefore ring in WHITE, the colour every heat
 * step in the palette was already contrast-checked against.
 */
export const MATRIX_CELL_BASE =
  'px-0.5 py-1 text-center text-xs tabular-nums align-middle ' +
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-white focus-visible:ring-inset';

interface SplitBand {
  key: keyof TimeSplit;
  color: string;
  labelKey: string;
  pattern: CSSProperties | null;
}

/**
 * The three Time-split bands plus the remainder, in the order the bar stacks
 * them. `noData` is DRAWN rather than dropped: a bar that stopped at 70 %
 * would read as a rendering bug, and the sparse pattern says which 30 % it is.
 */
const SPLIT_BANDS: readonly SplitBand[] = [
  {
    key: 'printing',
    color: TIME_SPLIT_BAND_COLOR.printing,
    labelKey: 'fleetMetrics.matrix.legend.printingBand',
    pattern: null,
  },
  {
    key: 'other',
    color: TIME_SPLIT_BAND_COLOR.other,
    labelKey: 'fleetMetrics.matrix.legend.otherBand',
    pattern: null,
  },
  {
    key: 'down',
    color: TIME_SPLIT_BAND_COLOR.down,
    labelKey: 'fleetMetrics.matrix.legend.downBand',
    pattern: FLEET_PATTERN_CSS.hatch,
  },
  {
    key: 'noData',
    color: FLEET_ABSENCE_COLOR.unobserved,
    labelKey: 'fleetMetrics.class.unobserved',
    pattern: FLEET_PATTERN_CSS.sparse,
  },
];

export interface FleetMatrixCellProps {
  /** Absent when the payload carries no cell for this printer-bucket pair. */
  cell: MatrixCell | undefined;
  /** The bucket's own header — `cellAbsence` reads observed vs elapsed off it. */
  header: AbsenceHeader;
  lens: FleetLens;
  /** The bucket width the SERVER echoed, never the one the client asked for. */
  bucketWidth: FleetBucket;
  /** `useTheme().resolvedMode`, passed down — a heat ramp has two directions. */
  mode: ResolvedThemeMode;
  /** The busiest printer cell in the window: the Prints ramp's denominator. */
  printsMax: number;
  /**
   * Heat this cell. False on the fleet summary row, whose magnitudes are the
   * sum of every printer's: a printer ramp applied there paints the whole row
   * at the cap and says nothing.
   */
  heat: boolean;
  /** Hour columns are 14 px wide: the figure rides sr-only text instead. */
  valueHidden: boolean;
  tabIndex: number;
  cellRef?: Ref<HTMLTableCellElement>;
  onFocus: () => void;
  onClick?: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLTableCellElement>) => void;
  className?: string;
}

export function FleetMatrixCell({
  cell,
  header,
  lens,
  bucketWidth,
  mode,
  printsMax,
  heat,
  valueHidden,
  tabIndex,
  cellRef,
  onFocus,
  onClick,
  onKeyDown,
  className = '',
}: FleetMatrixCellProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;

  // No inline WIDTH here: the table's `<colgroup>` is the ONE place a column's
  // width is stated. A cell that also declared one would out-specify the
  // `<col>` and put the frozen block's offsets back out of step with reality.
  const style: CSSProperties = {};
  /** The visible figure, if the cell has one. */
  let text: string | null = null;
  /** Why this cell has no ordinary number, spoken but not shown. */
  let reason: string | null = null;
  let bar: TimeSplit | null = null;
  /** An opaque heat tile brings its own proven text colour. */
  let painted = false;
  let dim = false;

  const absence = cell === undefined ? 'before_recording' : cellAbsence(cell, header, { lens });

  if (cell === undefined || absence === 'before_recording') {
    text = '–';
    dim = true;
    reason = t('fleetMetrics.class.not_recorded');
  } else if (absence === 'out_of_fleet') {
    // Flat ground: not a value and not a zero — the printer was not in the fleet.
    style.backgroundColor = 'var(--bg-primary)';
    reason = t('fleetMetrics.class.out_of_fleet');
  } else {
    if (absence === 'partial') {
      Object.assign(style, FLEET_PATTERN_CSS.sparse);
      reason = t(BASIS_LABEL_KEY[cell.basis]);
    }
    dim = absence === 'zero';

    if (lens === 'time_split') {
      bar = foldTimeSplit(cell.class_seconds);
    } else {
      const value =
        lens === 'hours_down' ? cell.down_seconds / SECONDS_PER_HOUR : sumMap(cell.prints);
      text =
        lens === 'hours_down' && value > 0 ? formatHours(value, locale) : formatCount(value, locale);
      const step =
        !heat || value <= 0
          ? 0
          : lens === 'hours_down'
            ? hoursDownBand(value, bucketWidth)
            : printsBand(value, printsMax);
      const paint = step > 0 ? lensRamp(lens, mode)[step] : undefined;
      if (paint) {
        style.backgroundColor = paint.background;
        style.color = paint.text;
        painted = true;
      }
    }
  }

  const spoken = [valueHidden && text !== null ? text : null, reason].filter(Boolean).join(' ');

  return (
    <td
      ref={cellRef}
      tabIndex={tabIndex}
      onFocus={onFocus}
      onClick={onClick}
      onKeyDown={onKeyDown}
      style={style}
      className={`${MATRIX_CELL_BASE} ${painted ? '' : dim ? SECONDARY_TEXT_CLASS : ''} ${className}`}
    >
      {bar ? <FleetSplitBar split={bar} /> : null}
      {text !== null && !valueHidden ? <span>{text}</span> : null}
      {spoken ? <span className="sr-only">{spoken}</span> : null}
    </td>
  );
}

export interface FleetSplitBarProps {
  split: TimeSplit;
  /** Bar height. The matrix cell is 12 px; the bucket detail's is 16 px. */
  heightClass?: string;
}

/**
 * THE Time-split bar — one tiny stacked bar, printing | other | down | no
 * data. Categorical, not a magnitude scale, so the bands never change colour
 * with the value and one palette serves both themes.
 *
 * Exported because the bucket detail draws the same bar full width: two
 * markups for one encoding is how a legend and a chart end up disagreeing.
 */
export function FleetSplitBar({ split, heightClass = 'h-3' }: FleetSplitBarProps) {
  const { t, i18n } = useTranslation();
  const share = (value: number): number => (split.total > 0 ? (value / split.total) * 100 : 0);
  const present = SPLIT_BANDS.filter((band) => share(split[band.key]) > 0);

  return (
    <>
      <span
        className={`flex ${heightClass} w-full overflow-hidden rounded-[2px]`}
        aria-hidden="true"
      >
        {present.map((band) => (
          <span
            key={band.key}
            style={{
              width: `${share(split[band.key])}%`,
              backgroundColor: band.color,
              // `currentColor` drives the pattern strokes; the bands were
              // contrast-checked against dark text.
              color: '#14161a',
              ...(band.pattern ?? {}),
            }}
          />
        ))}
      </span>
      <span className="sr-only">
        {present
          .map(
            (band) =>
              `${formatPercent(share(split[band.key]) / 100, i18n.language)} ${t(band.labelKey)}`,
          )
          .join(', ')}
      </span>
    </>
  );
}
