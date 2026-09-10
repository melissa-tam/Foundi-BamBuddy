/**
 * One selectable roll in the "Assign spool" picker.
 *
 * Extracted so the local-inventory branch and the Spoolman branch of
 * `AssignSpoolModal` render the identical card: the two copies had already
 * started to drift (only the local one carried the stale-claim annotation), and
 * every future line — the recency breadcrumb here being the first — would have
 * had to be added twice.
 */
import { MinusCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { InventorySpool } from '../api/client';
import { getSwatchStyle } from '../utils/colors';
import { remainingGrams } from '../utils/spoolGrams';
import type { SpoolRecency } from '../utils/spoolPicker';

interface SpoolPickerCardProps {
  spool: InventorySpool;
  selected: boolean;
  onSelect: () => void;
  /**
   * Set when this roll still holds a binding on some OTHER slot that reads
   * empty (W5b) — the sentence naming where that claim lives. Picking the roll
   * here MOVES the binding, so the operator has to be told the claim exists.
   */
  staleClaimLabel?: string;
  /** Which slot breadcrumb this row won, if any (`utils/spoolPicker`). */
  recency?: SpoolRecency;
}

export function SpoolPickerCard({ spool, selected, onSelect, staleClaimLabel, recency }: SpoolPickerCardProps) {
  const { t } = useTranslation();
  return (
    <button
      onClick={onSelect}
      title={spool.note || undefined}
      className={`p-2.5 rounded-lg border text-left transition-colors ${
        selected
          ? 'bg-bambu-green/20 border-bambu-green'
          : 'bg-bambu-dark border-bambu-dark-tertiary hover:border-bambu-gray'
      }`}
    >
      <p className="text-white text-sm font-medium truncate">
        {spool.brand ? `${spool.brand} ` : ''}{spool.material}{spool.subtype ? ` ${spool.subtype}` : ''}
      </p>
      <div className="flex items-center gap-1.5 mt-1">
        {spool.rgba && (
          <span
            className="w-3 h-3 rounded-full border border-black/20 flex-shrink-0"
            style={getSwatchStyle(spool.rgba)}
          />
        )}
        <span className="text-xs text-bambu-gray truncate">{spool.color_name || ''}</span>
      </div>
      {spool.label_weight && (
        <p className="text-xs text-bambu-gray mt-1">
          {Math.round(remainingGrams(spool))} / {spool.label_weight}g
        </p>
      )}
      {recency && (
        // `text-bambu-gray-light` (#a0a0a0) is 6.6:1 on `bg-bambu-dark` — AA at
        // this size. Deliberately NOT the note line's `text-bambu-gray/70`
        // below, which is sub-AA; do not model new lines on it.
        <p className="text-[10px] text-bambu-gray-light mt-1 truncate">
          {t(`inventory.assignRecency.${recency}`)}
        </p>
      )}
      {staleClaimLabel && (
        <p className="text-[10px] text-amber-400/90 mt-1 flex items-center gap-1" title={staleClaimLabel}>
          <MinusCircle className="w-3 h-3 shrink-0" aria-hidden="true" />
          <span className="truncate">{staleClaimLabel}</span>
        </p>
      )}
      {spool.note && (
        <p className="text-[10px] text-bambu-gray/70 mt-1 truncate" title={spool.note}>
          {spool.note}
        </p>
      )}
    </button>
  );
}
