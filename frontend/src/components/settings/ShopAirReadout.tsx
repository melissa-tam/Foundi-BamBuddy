/**
 * ShopAirReadout — the first row of Settings → Farm → "Eject cooldown": the
 * measured shop air and the eject line the farm arms every cooldown watch with,
 * as two labelled values ("Shop air" 25.8 °C, "Eject line" 27.8 °C).
 *
 * Everything shown is the server's (`useShopAir`, the owner's one read); this
 * component only formats it. HOW the value is known — measured now, carried
 * along the day curve, or not known at all — is supplementary, so it rides the
 * `InfoHint` beside "Shop air", never inline (react-best-practices §9). With no
 * reading the values say so ("no reading" / "at plateau": a watch armed without
 * a line releases when the bed stops cooling against its own air).
 *
 * The pair sits side by side and stacks when the CARD is narrower than `@xs`
 * (20rem) — a container query against the Eject cooldown card's `@container`,
 * because the Farm tab's half-width column makes the card ~400 px at a 1280
 * desktop while the viewport still reads `lg`.
 *
 * Each value is a `<dd>` named by its own caption (`aria-labelledby` the label
 * span, NOT the `<dt>` — the `<dt>` also holds the InfoHint button, whose
 * accessible name would otherwise leak into the value's name).
 */
import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import type { ShopAirResponse } from '../../api/client';
import { useShopAir } from '../../hooks/useShopAir';
import { InfoHint } from '../ui/InfoHint';
import {
  formatDateTime,
  formatTimeOnly,
  localDateKey,
  parseUTCDate,
  type TimeFormat,
} from '../../utils/date';

interface ShopAirReadoutProps {
  /** The operator's clock preference, for the carried sample's time. */
  timeFormat?: TimeFormat;
}

/** Shown while the first read is in flight or has failed — a symbol, not copy. */
const NO_VALUE = '—';

export function ShopAirReadout({ timeFormat = 'system' }: ShopAirReadoutProps) {
  const { t, i18n } = useTranslation();
  const { data, isError } = useShopAir();
  const shopLabelId = useId();
  const lineLabelId = useId();

  const celsius = (value: number) =>
    `${new Intl.NumberFormat(i18n.language, {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }).format(value)} °C`;

  /** The sample's time as the operator reads a clock: time only when it is from
   *  today, else the short date too (a carried sample can be up to 7 days old). */
  const sampleTime = (asOf: string | null): string => {
    const at = parseUTCDate(asOf);
    if (!at) return NO_VALUE;
    if (localDateKey(at) === localDateKey(new Date())) return formatTimeOnly(at, timeFormat);
    return formatDateTime(asOf, timeFormat, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const basisHint = (reading: ShopAirResponse): string => {
    switch (reading.basis) {
      case 'fresh': {
        const at = parseUTCDate(reading.as_of);
        const minutes = at ? Math.max(0, Math.floor((Date.now() - at.getTime()) / 60_000)) : 0;
        return t('settings.shopAir.fresh', { count: reading.printers, minutes });
      }
      case 'carried':
        return t('settings.shopAir.carried', { time: sampleTime(reading.as_of) });
      case 'unknown':
        return t('settings.shopAir.unknown');
    }
  };

  const shopText = data
    ? data.value_c === null
      ? t('settings.shopAir.noReading')
      : celsius(data.value_c)
    : NO_VALUE;
  const lineText = data
    ? data.eject_line_c === null
      ? t('settings.shopAir.atPlateau')
      : celsius(data.eject_line_c)
    : NO_VALUE;
  const hint = data ? basisHint(data) : isError ? t('settings.shopAir.readFailed') : null;

  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 @xs:grid-cols-2">
      <div>
        <dt className="flex items-center gap-1.5">
          <span id={shopLabelId} className="text-xs text-bambu-gray">
            {t('settings.shopAir.label')}
          </span>
          {hint !== null && <InfoHint text={hint} />}
        </dt>
        <dd aria-labelledby={shopLabelId} className="text-lg font-semibold text-white tabular-nums">
          {shopText}
        </dd>
      </div>
      <div>
        <dt id={lineLabelId} className="text-xs text-bambu-gray">
          {t('settings.shopAir.ejectLine')}
        </dt>
        <dd aria-labelledby={lineLabelId} className="text-lg font-semibold text-white tabular-nums">
          {lineText}
        </dd>
      </div>
    </dl>
  );
}
