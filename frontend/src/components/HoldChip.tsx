/**
 * HoldChip — the printer card's ONE hold pill, rendered from the backend's verdict
 * (`PrinterStatus.hold_chip`, built by `services/hold_chip`): which hold it names, its
 * tone and its words are decided there, from the live feed and the farm's own record —
 * this component decides nothing.
 *
 * The pill carries only the noun (one label per control). What the hold asks for, or
 * what the farm is doing about it, is supplementary detail and rides the accessible
 * `InfoHint` tooltip — shown on hover, keyboard focus and tap — never inline and never
 * a native `title` (react-best-practices §9). Amber while the farm still acts, red once
 * it is a person's turn; the tone is never the only signal, the label and tooltip say
 * the same.
 */
import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { InfoHint } from './ui/InfoHint';
import type { HoldChipState } from '../api/client';

const TONE_CLASS: Record<HoldChipState['tone'], string> = {
  acting: 'bg-yellow-500/20 text-yellow-400',
  held: 'bg-status-error/20 text-status-error',
};

interface HoldChipProps {
  chip: HoldChipState;
}

export function HoldChip({ chip }: HoldChipProps) {
  const { t } = useTranslation();
  const sentences = chip.tooltip.map((copy) => t(copy.key, copy.values)).join(' ');
  const tooltip = chip.qualifier ? `${sentences} — ${chip.qualifier}` : sentences;

  return (
    <span className={`flex items-center gap-1 px-2 py-1 rounded-full text-xs ${TONE_CLASS[chip.tone]}`}>
      <AlertTriangle className="w-3 h-3" aria-hidden="true" />
      {t(chip.label.key, chip.label.values)}
      <InfoHint text={tooltip} />
    </span>
  );
}
