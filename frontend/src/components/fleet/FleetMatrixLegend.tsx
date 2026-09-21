/**
 * The fleet matrix's legend — lens-specific, because the three lenses encode
 * three different things and one combined key would be a lie about two of them.
 *
 *   - **Prints** — a relative green ramp. The scale is stated (`0 to 42`)
 *     because the ramp rescales per window: without the maximum, a dark tile
 *     means nothing.
 *   - **Hours down** — an ABSOLUTE red ramp whose band edges are scaled to the
 *     echoed bucket. Four hours down is four hours down whether the fleet had
 *     a good month or a terrible one, so the swatches are labelled with the
 *     top edge rather than with the window.
 *   - **Time split** — three categorical bands. The down swatch carries the
 *     45° hatch it carries in the bar, because a legend that drops the pattern
 *     makes the third encoding channel unverifiable.
 */
import { useTranslation } from 'react-i18next';
import type { FleetBucket } from '../../types/fleetMetrics';
import {
  FLEET_PATTERN_CSS,
  SECONDARY_TEXT_CLASS,
  TIME_SPLIT_BAND_COLOR,
  formatCount,
  formatHours,
  hoursDownEdges,
  lensRamp,
  type FleetLens,
  type ResolvedThemeMode,
} from '../../utils/fleetMetrics';

export interface FleetMatrixLegendProps {
  lens: FleetLens;
  /** The bucket width the server echoed — the hours-down edges scale to it. */
  bucketWidth: FleetBucket;
  mode: ResolvedThemeMode;
  /** The busiest printer cell in the window: what the Prints ramp is scaled to. */
  printsMax: number;
}

export function FleetMatrixLegend({ lens, bucketWidth, mode, printsMax }: FleetMatrixLegendProps) {
  const { t, i18n } = useTranslation();
  const locale = i18n.language;

  if (lens === 'time_split') {
    const bands = [
      { key: 'printing', color: TIME_SPLIT_BAND_COLOR.printing, label: t('fleetMetrics.matrix.legend.printingBand'), pattern: null },
      { key: 'other', color: TIME_SPLIT_BAND_COLOR.other, label: t('fleetMetrics.matrix.legend.otherBand'), pattern: null },
      { key: 'down', color: TIME_SPLIT_BAND_COLOR.down, label: t('fleetMetrics.matrix.legend.downBand'), pattern: FLEET_PATTERN_CSS.hatch },
    ];
    return (
      <ul className={`flex flex-wrap items-center gap-x-4 gap-y-1 text-xs ${SECONDARY_TEXT_CLASS}`}>
        {bands.map((band) => (
          <li key={band.key} className="flex items-center gap-1.5">
            <span
              aria-hidden="true"
              className="inline-block h-3 w-3 rounded-[2px]"
              style={{ backgroundColor: band.color, color: '#14161a', ...(band.pattern ?? {}) }}
            />
            {band.label}
          </li>
        ))}
      </ul>
    );
  }

  const ramp = lensRamp(lens, mode);
  const topEdge = hoursDownEdges(bucketWidth)[2];

  return (
    <p className={`flex flex-wrap items-center gap-2 text-xs ${SECONDARY_TEXT_CLASS}`}>
      {lens === 'hours_down' ? <span>{t('fleetMetrics.matrix.legend.none')}</span> : null}
      <span className="inline-flex items-center gap-0.5" aria-hidden="true">
        {ramp.map((step) => (
          <span
            key={step.background}
            className="inline-block h-3 w-4 rounded-[2px]"
            style={{ backgroundColor: step.background }}
          />
        ))}
      </span>
      <span>
        {lens === 'hours_down'
          ? t('fleetMetrics.matrix.legend.over', { hours: formatHours(topEdge, locale) })
          : t('fleetMetrics.matrix.legend.scale', { max: formatCount(printsMax, locale) })}
      </span>
    </p>
  );
}
